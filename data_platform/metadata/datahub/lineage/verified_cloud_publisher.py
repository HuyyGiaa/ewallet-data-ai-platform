"""Publish only cloud-discovered DataHub metadata with semantic readback."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol


PLATFORM_URN = "urn:li:dataPlatform:gcs"
FLOW_CLUSTER = "ewallet-gcp"


class Adapter(Protocol):
    def put(self, urn: str, aspect: str, payload: dict[str, Any]) -> None: ...
    def get(self, urn: str, aspect: str) -> dict[str, Any] | None: ...


def flow_urn(dag_id: str) -> str:
    return f"urn:li:dataFlow:(airflow,{dag_id},{FLOW_CLUSTER})"


def job_urn(dag_id: str, task_id: str) -> str:
    return f"urn:li:dataJob:({flow_urn(dag_id)},{task_id})"


def _schema_hash(fields: list[dict[str, Any]]) -> str:
    value = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def records(inventory: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    if inventory.get("mode") != "READ_ONLY_REAL_CLOUD_DISCOVERY":
        raise ValueError("publisher requires a real cloud discovery inventory")
    values: list[tuple[str, str, dict[str, Any]]] = []
    discovered = {item["urn"] for item in inventory["datasets"] if item.get("physical_exists")}
    if len(discovered) != inventory["dataset_count"]:
        raise ValueError("not every inventory dataset was physically verified")
    for item in inventory["datasets"]:
        fields = item["schema_fields"]
        values.append((item["urn"], "datasetProperties", {
            "name": f"{item['zone']}.{item['table']}",
            "qualifiedName": item["uri"],
            "description": f"Verified {item['format']} dataset discovered read-only from cloud metadata",
            "customProperties": {
                "zone": item["zone"], "format": item["format"],
                "physical_exists": "true", "source_git_sha": inventory["source_git_sha"],
                "file_count": str(item["file_count"]), "gcs_uri": item["uri"],
            },
        }))
        values.append((item["urn"], "schemaMetadata", {
            "schemaName": f"{item['zone']}.{item['table']}",
            "platform": PLATFORM_URN, "version": int(item.get("delta_version", 0)),
            "hash": _schema_hash(fields), "rawSchema": json.dumps(fields, sort_keys=True),
            "fields": [
                {
                    "fieldPath": field["name"], "nativeDataType": field["type"],
                    "nullable": bool(field["nullable"]),
                    "isPartitioningKey": field["name"] in item["partition_columns"],
                }
                for field in fields
            ],
        }))

    seen_flows: set[str] = set()
    for job in inventory["jobs"]:
        if not set(job["inputs"] + job["outputs"]) <= discovered:
            raise ValueError(f"job references an undiscovered dataset: {job['batch_id']}")
        flow = flow_urn(job["dag_id"])
        if flow not in seen_flows:
            values.append((flow, "dataFlowInfo", {
                "name": job["dag_id"], "env": "PROD",
                "customProperties": {
                    "orchestrator": "airflow", "cluster": FLOW_CLUSTER,
                    "metadata_source": "verified_gcs_delta_dataproc",
                },
            }))
            seen_flows.add(flow)
        values.append((job_urn(job["dag_id"], job["task_id"]), "dataJobInfo", {
            "name": job["task_id"], "type": "BATCH", "flowUrn": flow,
            "customProperties": {
                "dataproc_batch_id": job["batch_id"], "batch_state": job["batch_state"],
                "source_git_sha": job["git_sha"],
            },
        }))
        values.append((job_urn(job["dag_id"], job["task_id"]), "dataJobInputOutput", {
            "inputDatasets": sorted(job["inputs"]), "outputDatasets": sorted(job["outputs"]),
        }))
    for edge in inventory["lineage"]:
        if edge["downstream"] not in discovered or not set(edge["upstreams"]) <= discovered:
            raise ValueError("lineage references an undiscovered dataset")
        values.append((edge["downstream"], "upstreamLineage", {
            "upstreams": [{"dataset": urn, "type": "TRANSFORMED"} for urn in sorted(edge["upstreams"])]
        }))
    return values


def publish_with_readback(inventory: dict[str, Any], adapter: Adapter) -> dict[str, Any]:
    verified = []
    for urn, aspect, payload in records(inventory):
        adapter.put(urn, aspect, payload)
        observed = adapter.get(urn, aspect)
        if observed != payload:
            raise RuntimeError(f"semantic readback mismatch urn={urn} aspect={aspect}")
        verified.append({"urn": urn, "aspect": aspect, "readback": "MATCH"})
    return {"published": len(verified), "verified": verified}


class DataHubAdapter:
    def __init__(self, url: str, token: str | None = None) -> None:
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
        config = DatahubClientConfig(server=url, token=token)
        self.graph = DataHubGraph(config)
        self.emitter = DatahubRestEmitter(gms_server=url, token=token)
        self.graph.test_connection()

    @staticmethod
    def _field_type(native: str) -> Any:
        from datahub.metadata.schema_classes import (
            ArrayTypeClass, BooleanTypeClass, BytesTypeClass, DateTypeClass,
            NumberTypeClass, SchemaFieldDataTypeClass, StringTypeClass, TimeTypeClass,
        )
        value = native.lower()
        if "bool" in value:
            kind = BooleanTypeClass()
        elif any(word in value for word in ("int", "float", "double", "decimal")):
            kind = NumberTypeClass()
        elif value == "date32[day]" or value == "date":
            kind = DateTypeClass()
        elif "timestamp" in value or value.startswith("time"):
            kind = TimeTypeClass()
        elif "binary" in value or "bytes" in value:
            kind = BytesTypeClass()
        elif value.startswith("list") or value.startswith("array"):
            kind = ArrayTypeClass()
        else:
            kind = StringTypeClass()
        return SchemaFieldDataTypeClass(type=kind)

    def put(self, urn: str, aspect: str, payload: dict[str, Any]) -> None:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata.schema_classes import (
            DataFlowInfoClass, DataJobInfoClass, DataJobInputOutputClass,
            DatasetLineageTypeClass, DatasetPropertiesClass, OtherSchemaClass,
            SchemaFieldClass, SchemaMetadataClass, UpstreamClass, UpstreamLineageClass,
        )
        if aspect == "datasetProperties":
            value = DatasetPropertiesClass(**payload)
        elif aspect == "schemaMetadata":
            value = SchemaMetadataClass(
                schemaName=payload["schemaName"], platform=payload["platform"],
                version=payload["version"], hash=payload["hash"],
                platformSchema=OtherSchemaClass(rawSchema=payload["rawSchema"]),
                fields=[SchemaFieldClass(
                    fieldPath=field["fieldPath"], nativeDataType=field["nativeDataType"],
                    nullable=field["nullable"], isPartitioningKey=field["isPartitioningKey"],
                    type=self._field_type(field["nativeDataType"]),
                ) for field in payload["fields"]],
            )
        elif aspect == "dataFlowInfo":
            value = DataFlowInfoClass(**payload)
        elif aspect == "dataJobInfo":
            value = DataJobInfoClass(**payload)
        elif aspect == "dataJobInputOutput":
            value = DataJobInputOutputClass(**payload)
        elif aspect == "upstreamLineage":
            value = UpstreamLineageClass(upstreams=[UpstreamClass(
                dataset=item["dataset"], type=DatasetLineageTypeClass.TRANSFORMED,
            ) for item in payload["upstreams"]])
        else:
            raise ValueError(aspect)
        self.emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=value))

    def get(self, urn: str, aspect: str) -> dict[str, Any] | None:
        from datahub.metadata.schema_classes import (
            DataFlowInfoClass, DataJobInfoClass, DataJobInputOutputClass,
            DatasetPropertiesClass, SchemaMetadataClass, UpstreamLineageClass,
        )
        classes = {
            "datasetProperties": DatasetPropertiesClass, "schemaMetadata": SchemaMetadataClass,
            "dataFlowInfo": DataFlowInfoClass, "dataJobInfo": DataJobInfoClass,
            "dataJobInputOutput": DataJobInputOutputClass, "upstreamLineage": UpstreamLineageClass,
        }
        value = self.graph.get_aspect(entity_urn=urn, aspect_type=classes[aspect])
        if value is None:
            return None
        if aspect == "datasetProperties":
            return {"name": value.name, "qualifiedName": value.qualifiedName,
                    "description": value.description, "customProperties": dict(value.customProperties or {})}
        if aspect == "schemaMetadata":
            return {"schemaName": value.schemaName, "platform": value.platform,
                    "version": value.version, "hash": value.hash,
                    "rawSchema": value.platformSchema.rawSchema,
                    "fields": [{"fieldPath": f.fieldPath, "nativeDataType": f.nativeDataType,
                                "nullable": bool(f.nullable), "isPartitioningKey": bool(f.isPartitioningKey)}
                               for f in value.fields]}
        if aspect == "dataFlowInfo":
            return {"name": value.name, "env": str(value.env), "customProperties": dict(value.customProperties or {})}
        if aspect == "dataJobInfo":
            return {"name": value.name, "type": str(value.type), "flowUrn": value.flowUrn,
                    "customProperties": dict(value.customProperties or {})}
        if aspect == "dataJobInputOutput":
            return {"inputDatasets": sorted(value.inputDatasets or []),
                    "outputDatasets": sorted(value.outputDatasets or [])}
        return {"upstreams": sorted([{"dataset": item.dataset, "type": str(item.type)}
                                     for item in value.upstreams], key=lambda item: item["dataset"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gms-url")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    generated = records(inventory)
    if args.publish:
        if not args.gms_url:
            parser.error("--gms-url is required with --publish")
        import os
        payload = {"mode": "PUBLISH_REAL_CLOUD_METADATA_WITH_READBACK",
                   **publish_with_readback(inventory, DataHubAdapter(args.gms_url, os.getenv("DATAHUB_GMS_TOKEN")))}
    else:
        payload = {"mode": "DRY_RUN_REAL_CLOUD_METADATA", "record_count": len(generated),
                   "records": [{"urn": u, "aspect": a, "payload": p} for u, a, p in generated]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
