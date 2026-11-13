"""Publish verified campaign metadata to DataHub with semantic readback.

``records`` turns a verified-metadata document (built by
``lineage/batch_lineage.py`` from checked campaign evidence) into DataHub aspects:
``datasetProperties`` and, when an observed schema is present, ``schemaMetadata``
for every dataset; ``dataFlowInfo``, ``dataJobInfo`` and ``dataJobInputOutput``
for every Airflow task; ``upstreamLineage`` for every dataset edge. It refuses a
document whose jobs or edges name a dataset the document does not declare.

``publish_with_readback`` writes each aspect and reads it back, failing on the first
difference. ``DataHubAdapter`` is the SDK adapter; it is also reused by the RAG
ingestion governance (``ai_platform/llm_agent/rag/governance.py``). The SDK is
imported only when the adapter is constructed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Protocol

from data_platform.metadata.datahub.urns import ENVIRONMENT, FLOW_CLUSTER, flow_urn, job_urn


PLATFORM_URN = "urn:li:dataPlatform:gcs"
CAMPAIGN_MODE = "VERIFIED_CAMPAIGN_EVIDENCE"
__all__ = ["CAMPAIGN_MODE", "DataHubAdapter", "flow_urn", "job_urn", "publish_with_readback", "records"]


class Adapter(Protocol):
    """The two operations the publisher needs from DataHub."""

    def put(self, urn: str, aspect: str, payload: dict[str, Any]) -> None: ...
    def get(self, urn: str, aspect: str) -> dict[str, Any] | None: ...


def _schema_hash(fields: list[dict[str, Any]]) -> str:
    value = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dataset_records(item: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    values: list[tuple[str, str, dict[str, Any]]] = [(item["urn"], "datasetProperties", {
        "name": item["name"],
        "qualifiedName": item["qualified_name"],
        "description": item["description"],
        "customProperties": {key: str(value) for key, value in item["custom_properties"].items()},
    })]
    schema = item.get("schema")
    if schema:
        fields = [dict(field) for field in schema["fields"]]
        values.append((item["urn"], "schemaMetadata", {
            "schemaName": item["name"],
            "platform": PLATFORM_URN,
            "version": int(schema.get("version", 0)),
            "hash": _schema_hash(fields),
            "rawSchema": json.dumps(fields, sort_keys=True),
            "fields": [
                {
                    "fieldPath": field["name"],
                    "nativeDataType": str(field["type"]),
                    "nullable": bool(field["nullable"]),
                    "isPartitioningKey": field["name"] in schema.get("partition_columns", ()),
                }
                for field in fields
            ],
        }))
    return values


def records(document: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Return every aspect of a verified-metadata document, deterministically ordered.

    Raises:
        ValueError: The document is not verified campaign evidence, a dataset is
            declared twice, or a job or edge names an undeclared dataset.
    """

    if document.get("mode") != CAMPAIGN_MODE:
        raise ValueError(f"publisher requires a {CAMPAIGN_MODE} document, got {document.get('mode')!r}")
    declared = [item["urn"] for item in document["datasets"]]
    if len(declared) != len(set(declared)):
        raise ValueError("a dataset is declared twice")
    known = set(declared)
    values: list[tuple[str, str, dict[str, Any]]] = []
    for item in document["datasets"]:
        values.extend(_dataset_records(item))
    seen_flows: set[str] = set()
    for job in document["jobs"]:
        undeclared = set(job["inputs"]) | set(job["outputs"])
        if not undeclared <= known:
            raise ValueError(f"job {job['dag_id']}.{job['task_id']} names undeclared datasets: "
                             f"{sorted(undeclared - known)}")
        flow = flow_urn(job["dag_id"])
        if flow not in seen_flows:
            values.append((flow, "dataFlowInfo", {
                "name": job["dag_id"],
                "env": ENVIRONMENT,
                "customProperties": {"cluster": FLOW_CLUSTER, **job["flow_custom_properties"]},
            }))
            seen_flows.add(flow)
        task = job_urn(job["dag_id"], job["task_id"])
        values.append((task, "dataJobInfo", {
            "name": job["task_id"], "type": "BATCH", "flowUrn": flow,
            "customProperties": dict(job["job_custom_properties"]),
        }))
        values.append((task, "dataJobInputOutput", {
            "inputDatasets": sorted(job["inputs"]), "outputDatasets": sorted(job["outputs"]),
        }))
    for edge in document["lineage"]:
        if edge["downstream"] not in known or not set(edge["upstreams"]) <= known:
            raise ValueError(f"lineage edge to {edge['downstream']} names an undeclared dataset")
        values.append((edge["downstream"], "upstreamLineage", {
            "upstreams": [{"dataset": urn, "type": "TRANSFORMED"} for urn in sorted(edge["upstreams"])]
        }))
    return values


def publish_with_readback(document: Mapping[str, Any], adapter: Adapter) -> dict[str, Any]:
    """Write every aspect and require its readback to match exactly.

    Writing the same document twice writes the same aspects again, so publication
    is idempotent.

    Raises:
        RuntimeError: An aspect reads back missing or different.
    """

    verified = []
    for urn, aspect, payload in records(document):
        adapter.put(urn, aspect, payload)
        observed = adapter.get(urn, aspect)
        if observed != payload:
            raise RuntimeError(f"semantic readback mismatch urn={urn} aspect={aspect}: "
                               f"expected={payload!r} observed={observed!r}")
        verified.append({"urn": urn, "aspect": aspect, "readback": "MATCH"})
    return {"published": len(verified), "verified": verified}


class DataHubAdapter:
    """DataHub 1.7 SDK adapter: writes one aspect, reads it back in normalized form."""

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
