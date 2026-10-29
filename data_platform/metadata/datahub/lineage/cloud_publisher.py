"""Idempotent publisher with mandatory DataHub aspect readback.

Publication is never implicit: callers must pass ``--publish`` and a GMS URL.
The pure publish contract is tested with an in-memory adapter; SDK imports are
deferred so ordinary CI does not require a running DataHub environment.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Protocol

from .cloud_lineage import build_plan


class AspectAdapter(Protocol):
    def put(self, urn: str, aspect: str, payload: dict[str, object]) -> None: ...
    def get(self, urn: str, aspect: str) -> dict[str, object] | None: ...


def aspect_records(plan: dict[str, object]) -> list[tuple[str, str, dict[str, object]]]:
    records: list[tuple[str, str, dict[str, object]]] = []
    seen_flows: set[str] = set()
    for job in plan["jobs"]:
        flow = job["flow_urn"]
        if flow not in seen_flows:
            records.append((flow, "dataFlowInfo", {
                "name": job["dag_id"],
                "orchestrator": plan["platform"],
                "cluster": plan["cluster"],
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
    return records


def publish_with_readback(
    plan: dict[str, object],
    adapter: AspectAdapter,
) -> dict[str, object]:
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
        )

        classes = {
            "dataFlowInfo": DataFlowInfoClass,
            "dataJobInfo": DataJobInfoClass,
            "dataJobInputOutput": DataJobInputOutputClass,
        }
        value = classes[aspect](**payload)
        self.emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=value))

    def get(self, urn: str, aspect: str) -> dict[str, object] | None:
        from datahub.metadata.schema_classes import (
            DataFlowInfoClass,
            DataJobInfoClass,
            DataJobInputOutputClass,
        )
        classes = {
            "dataFlowInfo": DataFlowInfoClass,
            "dataJobInfo": DataJobInfoClass,
            "dataJobInputOutput": DataJobInputOutputClass,
        }
        value = self.graph.get_aspect(entity_urn=urn, aspect_type=classes[aspect])
        if value is None:
            return None
        if aspect == "dataFlowInfo":
            return {key: getattr(value, key) for key in ("name", "orchestrator", "cluster")}
        if aspect == "dataJobInfo":
            return {key: getattr(value, key) for key in ("name", "type", "flowUrn")}
        return {
            "inputDatasets": sorted(value.inputDatasets or []),
            "outputDatasets": sorted(value.outputDatasets or []),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gms-url")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    plan = build_plan()
    if not args.publish:
        payload = {"mode": "DRY_RUN", "records": [
            {"urn": urn, "aspect": aspect, "payload": value}
            for urn, aspect, value in aspect_records(plan)
        ]}
    else:
        if not args.gms_url:
            parser.error("--gms-url is required with --publish")
        import os
        adapter = DataHubSdkAdapter(args.gms_url, os.getenv("DATAHUB_GMS_TOKEN"))
        payload = {"mode": "PUBLISH_WITH_READBACK", **publish_with_readback(plan, adapter)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
