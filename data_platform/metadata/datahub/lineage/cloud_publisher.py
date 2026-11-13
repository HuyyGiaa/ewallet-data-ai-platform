"""Generic plan-to-aspect translation and idempotent publication with readback.

Shared library, no command line. ``aspect_records`` turns a lineage plan (jobs with
DataFlow/DataJob URNs, inputs and outputs, plus dataset edges) into DataHub
aspects; ``publish_with_readback`` writes each one and requires an exact readback.
Two plans use it: the Feast materialization publisher
(``feast_materialization.py``, which also uses ``DataHubSdkAdapter``) and the
frozen historical 100GB plan in ``cloud_lineage.py`` that the datahub-publisher
image build exercises. The EDAI-1 5GB batch campaign is published by
``batch_lineage.py`` through ``verified_cloud_publisher.py`` instead, because it
also carries dataset properties and schemas.

The SDK is imported only when ``DataHubSdkAdapter`` is constructed.
"""

from __future__ import annotations

from typing import Protocol


class AspectAdapter(Protocol):
    """The two operations the publisher needs from DataHub."""

    def put(self, urn: str, aspect: str, payload: dict[str, object]) -> None: ...
    def get(self, urn: str, aspect: str) -> dict[str, object] | None: ...


def aspect_records(plan: dict[str, object]) -> list[tuple[str, str, dict[str, object]]]:
    """Return the DataFlow, DataJob and upstream-lineage aspects of one plan."""

    records: list[tuple[str, str, dict[str, object]]] = []
    seen_flows: set[str] = set()
    for job in plan["jobs"]:
        flow = job["flow_urn"]
        if flow not in seen_flows:
            records.append((flow, "dataFlowInfo", {
                "name": job["dag_id"],
                "customProperties": {
                    "orchestrator": str(plan["platform"]),
                    "cluster": str(plan["cluster"]),
                    "publication_mode": "retrospective_historical_run",
                },
                "env": str(plan["environment"]),
            }))
            seen_flows.add(flow)
        records.append((job["job_urn"], "dataJobInfo", {
            "name": job["task_id"],
            "type": "BATCH",
            "flowUrn": flow,
        }))
        records.append((job["job_urn"], "dataJobInputOutput", {
            "inputDatasets": sorted(job["inputs"]),
            "outputDatasets": sorted(job["outputs"]),
        }))
    for edge in plan["dataset_lineage"]:
        records.append((edge["downstream"], "upstreamLineage", {
            "upstreams": [
                {"dataset": urn, "type": "TRANSFORMED"}
                for urn in sorted(edge["upstreams"])
            ]
        }))
    return records


def publish_with_readback(
    plan: dict[str, object],
    adapter: AspectAdapter,
) -> dict[str, object]:
    """Write every aspect of ``plan`` and fail on the first readback mismatch."""

    records = aspect_records(plan)
    verified = []
    for urn, aspect, payload in records:
        adapter.put(urn, aspect, payload)
        observed = adapter.get(urn, aspect)
        if observed != payload:
            raise RuntimeError(
                f"DataHub readback mismatch for {urn} aspect={aspect}: "
                f"expected={payload!r}, observed={observed!r}"
            )
        verified.append({"urn": urn, "aspect": aspect, "readback": "MATCH"})
    return {"published": len(records), "verified": verified}


class DataHubSdkAdapter:
    """Thin SDK adapter; aspect payloads remain deterministic and testable."""

    def __init__(self, gms_url: str, token: str | None = None) -> None:
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig

        config = DatahubClientConfig(server=gms_url, token=token)
        self.graph = DataHubGraph(config)
        self.emitter = DatahubRestEmitter(gms_server=gms_url, token=token)
        self.graph.test_connection()

    def put(self, urn: str, aspect: str, payload: dict[str, object]) -> None:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata.schema_classes import (
            DataFlowInfoClass,
            DataJobInfoClass,
            DataJobInputOutputClass,
            DatasetLineageTypeClass,
            UpstreamClass,
            UpstreamLineageClass,
        )

        classes = {
            "dataFlowInfo": DataFlowInfoClass,
            "dataJobInfo": DataJobInfoClass,
            "dataJobInputOutput": DataJobInputOutputClass,
        }
        if aspect == "upstreamLineage":
            value = UpstreamLineageClass(
                upstreams=[
                    UpstreamClass(
                        dataset=item["dataset"],
                        type=DatasetLineageTypeClass.TRANSFORMED,
                    )
                    for item in payload["upstreams"]
                ]
            )
        else:
            value = classes[aspect](**payload)
        self.emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=value))

    def get(self, urn: str, aspect: str) -> dict[str, object] | None:
        from datahub.metadata.schema_classes import (
            DataFlowInfoClass,
            DataJobInfoClass,
            DataJobInputOutputClass,
            UpstreamLineageClass,
        )
        classes = {
            "dataFlowInfo": DataFlowInfoClass,
            "dataJobInfo": DataJobInfoClass,
            "dataJobInputOutput": DataJobInputOutputClass,
            "upstreamLineage": UpstreamLineageClass,
        }
        value = self.graph.get_aspect(entity_urn=urn, aspect_type=classes[aspect])
        if value is None:
            return None
        if aspect == "dataFlowInfo":
            return {
                "name": value.name,
                "customProperties": dict(value.customProperties or {}),
                "env": str(value.env),
            }
        if aspect == "dataJobInfo":
            result = {
                "name": value.name,
                "type": str(value.type),
                "flowUrn": value.flowUrn,
            }
            if value.customProperties:
                result["customProperties"] = dict(value.customProperties)
            return result
        if aspect == "upstreamLineage":
            return {
                "upstreams": sorted(
                    (
                        {"dataset": item.dataset, "type": str(item.type)}
                        for item in value.upstreams
                    ),
                    key=lambda item: item["dataset"],
                )
            }
        return {
            "inputDatasets": sorted(value.inputDatasets or []),
            "outputDatasets": sorted(value.outputDatasets or []),
        }
