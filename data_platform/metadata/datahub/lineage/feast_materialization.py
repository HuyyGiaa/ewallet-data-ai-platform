"""Publish current-run Feast materialization lineage with semantic readback."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.config.dataset_identity import resolve_storage_context
from data_platform.metadata.datahub.lineage.cloud_publisher import (
    DataHubSdkAdapter,
    aspect_records as generic_aspect_records,
)


ENVIRONMENT = "PROD"
FLOW_CLUSTER = "ewallet-gcp"


def _dataset_urn(platform: str, name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{ENVIRONMENT})"


def _flow_urn(dag_id: str) -> str:
    return f"urn:li:dataFlow:(airflow,{dag_id},{FLOW_CLUSTER})"


def _job_urn(dag_id: str) -> str:
    return f"urn:li:dataJob:({_flow_urn(dag_id)},materialize_window)"


def build_plan(provenance: dict[str, Any]) -> dict[str, Any]:
    """Build four exact Offline-to-Online edges from ``07-provenance.json``."""

    if provenance.get("schema_version") != "feast-incremental-provenance-v1":
        raise ValueError("unknown Feast provenance schema")
    if provenance.get("status") != "PASS":
        raise ValueError("DataHub publication requires PASS provenance")
    outcome = provenance.get("outcome")
    if outcome not in {"DRY_RUN", "NEW_WINDOW", "PARTIAL", "COMPLETED", "NO_NEW_DATA"}:
        raise ValueError("unknown materialization outcome")
    request = provenance.get("request")
    airflow = provenance.get("airflow")
    if not isinstance(request, dict) or not isinstance(airflow, dict):
        raise ValueError("provenance request and airflow identity are required")
    required_request = (
        "bucket",
        "dataset_version",
        "feature_run_id",
        "feast_snapshot_id",
        "online_registry_id",
        "online_store_id",
        "snapshot_root",
        "registry_uri",
        "target_end",
    )
    if any(not request.get(name) for name in required_request):
        raise ValueError("provenance request identity is incomplete")
    if not airflow.get("dag_id") or not airflow.get("run_id"):
        raise ValueError("Airflow DagRun identity is incomplete")
    storage = resolve_storage_context(str(request["dataset_version"]), str(request["bucket"]))
    expected_root = (
        f"gs://{storage.data_bucket}/features/offline/snapshots/"
        f"{request['dataset_version']}/{request['feature_run_id']}/{request['feast_snapshot_id']}"
    )
    if request["snapshot_root"] != expected_root:
        raise ValueError("snapshot root does not match the current dataset identity")
    window_id = str(provenance.get("window_id", ""))
    if len(window_id) != 12:
        raise ValueError("provenance window_id is invalid")

    decision = provenance.get("window_decision")
    if outcome == "DRY_RUN":
        affected_views = tuple(FEATURE_SPECS)
    else:
        if not isinstance(decision, dict) or decision.get("state") != outcome:
            raise ValueError("provenance window decision is missing or inconsistent")
        pending = decision.get("pending_views")
        if not isinstance(pending, list) or any(view not in FEATURE_SPECS for view in pending):
            raise ValueError("provenance pending FeatureViews are invalid")
        affected_views = tuple(pending)
        if outcome == "NEW_WINDOW" and set(affected_views) != set(FEATURE_SPECS):
            raise ValueError("NEW_WINDOW must materialize every FeatureView")
        if outcome == "PARTIAL" and not (0 < len(affected_views) < len(FEATURE_SPECS)):
            raise ValueError("PARTIAL must name a non-empty strict FeatureView subset")

    offline: dict[str, str] = {}
    online: dict[str, str] = {}
    for view, spec in FEATURE_SPECS.items():
        offline[view] = _dataset_urn("gcs", f"{expected_root}/{spec['table']}")
        online_name = (
            f"feast://{request['online_store_id']}/{request['dataset_version']}/"
            f"{request['online_registry_id']}/{view}"
        )
        online[view] = _dataset_urn("feast", online_name)

    dag_id = str(airflow["dag_id"])
    flow = _flow_urn(dag_id)
    job = {
        "dag_id": dag_id,
        "task_id": "materialize_window",
        "flow_urn": flow,
        "job_urn": _job_urn(dag_id),
        "inputs": sorted(offline[view] for view in affected_views),
        "outputs": sorted(online[view] for view in affected_views),
        "flow_custom_properties": {
            "orchestrator": "airflow",
            "cluster": FLOW_CLUSTER,
            "dag_run_id": str(airflow["run_id"]),
            "dataset_version": str(request["dataset_version"]),
        },
        "job_custom_properties": {
            "dag_run_id": str(airflow["run_id"]),
            "window_id": window_id,
            "dataset_version": str(request["dataset_version"]),
            "snapshot_id": str(request["feast_snapshot_id"]),
            "online_store_id": str(request["online_store_id"]),
            "provenance_digest": str(provenance.get("provenance_digest", "")),
            "git_sha": str(provenance.get("git_sha", "")),
            "target_end": str(request["target_end"]),
            "materialization_outcome": str(outcome),
            "window_start": str(decision.get("start", "")) if isinstance(decision, dict) else "",
            "window_end": str(decision.get("end", "")) if isinstance(decision, dict) else "",
            "materialized_views": ",".join(sorted(affected_views)),
        },
    }
    return {
        "platform": "airflow",
        "environment": ENVIRONMENT,
        "cluster": FLOW_CLUSTER,
        "dag_run_id": airflow["run_id"],
        "dataset_version": request["dataset_version"],
        "snapshot_id": request["feast_snapshot_id"],
        "online_store_id": request["online_store_id"],
        "window_id": window_id,
        "provenance_digest": provenance.get("provenance_digest"),
        "datasets": sorted([*offline.values(), *online.values()]),
        "jobs": [job],
        "dataset_lineage": [
            {"downstream": online[view], "upstreams": [offline[view]]}
            for view in affected_views
        ],
    }


def _records(plan: dict[str, Any]) -> list[tuple[str, str, dict[str, object]]]:
    """Translate the Feast plan while retaining current-run custom properties."""

    records = generic_aspect_records(plan)
    job = plan["jobs"][0]
    for index, (urn, aspect, payload) in enumerate(records):
        if aspect == "dataFlowInfo":
            payload["customProperties"] = dict(job["flow_custom_properties"])
        elif aspect == "dataJobInfo":
            payload["customProperties"] = dict(job["job_custom_properties"])
        records[index] = (urn, aspect, payload)
    return records


# Public alias kept explicit so tests and the orchestration contract share the
# exact records sent to DataHub.
aspect_records = _records


def publish_with_readback(plan: dict[str, Any], adapter: Any) -> dict[str, Any]:
    """Publish every Feast aspect and require exact semantic readback."""

    verified = []
    for urn, aspect, payload in aspect_records(plan):
        adapter.put(urn, aspect, payload)
        observed = adapter.get(urn, aspect)
        if observed != payload:
            raise RuntimeError(
                f"DataHub readback mismatch for {urn} aspect={aspect}: "
                f"expected={payload!r}, observed={observed!r}"
            )
        verified.append({"urn": urn, "aspect": aspect, "readback": "MATCH"})
    return {"published": len(verified), "verified": verified}


def result_document(plan: dict[str, Any], published: dict[str, Any] | None) -> dict[str, Any]:
    """Return the credential-free machine-readable stage result."""

    records = aspect_records(plan)
    if published is None:
        return {
            "status": "DRY_RUN",
            "dag_run_id": plan["dag_run_id"],
            "dataset_version": plan["dataset_version"],
            "snapshot_id": plan["snapshot_id"],
            "online_store_id": plan["online_store_id"],
            "published_urns": sorted({urn for urn, _, _ in records}),
            "aspect_results": [],
            "semantic_readback": {"verified": 0, "mismatches": 0},
            "verification_status": "DRY_RUN",
            "records": [
                {"urn": urn, "aspect": aspect, "payload": payload}
                for urn, aspect, payload in records
            ],
        }
    verified = list(published["verified"])
    return {
        "status": "PASS",
        "dag_run_id": plan["dag_run_id"],
        "dataset_version": plan["dataset_version"],
        "snapshot_id": plan["snapshot_id"],
        "online_store_id": plan["online_store_id"],
        "published_urns": sorted({item["urn"] for item in verified}),
        "aspect_results": verified,
        "semantic_readback": {"verified": len(verified), "mismatches": 0},
        "verification_status": "PASS",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--gms-url")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    provenance = json.loads(args.provenance.read_text(encoding="utf-8"))
    plan = build_plan(provenance)
    if args.publish:
        if not args.gms_url:
            parser.error("--gms-url is required with --publish")
        if provenance.get("outcome") not in {"NEW_WINDOW", "PARTIAL"}:
            raise ValueError("live lineage publication requires a materialized window")
        adapter = DataHubSdkAdapter(args.gms_url, os.getenv("DATAHUB_GMS_TOKEN"))
        result = result_document(plan, publish_with_readback(plan, adapter))
    else:
        result = result_document(plan, None)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
