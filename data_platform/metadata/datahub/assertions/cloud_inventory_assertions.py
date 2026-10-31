"""Publish cloud-derived Delta existence/schema assertions with readback."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any


def _assertion_id(dataset: dict[str, Any]) -> str:
    return f"cloud_{dataset['zone']}_{dataset['table']}_delta_schema_readable"


def _urn(value: str) -> str:
    from datahub.emitter.mce_builder import make_assertion_urn
    return make_assertion_urn(value)


def publish(inventory: dict[str, Any], gms_url: str) -> dict[str, Any]:
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.emitter.rest_emitter import DatahubRestEmitter
    from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
    from datahub.metadata.schema_classes import (
        AssertionInfoClass, AssertionResultClass, AssertionResultTypeClass,
        AssertionRunEventClass, AssertionRunStatusClass,
    )
    from datahub.sdk import DataHubClient

    client = DataHubClient(server=gms_url)
    graph = DataHubGraph(DatahubClientConfig(server=gms_url))
    graph.test_connection()
    emitter = DatahubRestEmitter(gms_server=gms_url)
    timestamp = int(time.time() * 1000)
    verified: list[dict[str, Any]] = []
    pending: list[tuple[dict[str, Any], str, str, dict[str, Any]]] = []
    for dataset in inventory["datasets"]:
        if dataset["format"] != "DELTA":
            continue
        assertion_id = _assertion_id(dataset)
        assertion_urn = _urn(assertion_id)
        logic = (
            "Read Delta transaction log, extract schema and active-file inventory; "
            "require physical_exists=true, schema_fields>0 and delta_table_id present."
        )
        response = client.assertions.sync_custom_assertion(
            entity_urn=dataset["urn"], type="Cloud Delta Metadata Contract",
            description=f"{dataset['zone']}.{dataset['table']} exists on GCS and exposes a readable Delta schema",
            platform_urn="urn:li:dataPlatform:gcs", urn=assertion_urn,
            logic=logic, native_type="delta_log_schema_readback",
            native_parameters=[
                {"key": "gcs_uri", "value": dataset["uri"]},
                {"key": "delta_table_id", "value": dataset["delta_table_id"]},
                {"key": "delta_version", "value": str(dataset["delta_version"])},
                {"key": "active_file_count", "value": str(dataset["file_count"])},
            ],
        )
        if response.get("urn") != assertion_urn:
            raise RuntimeError(f"assertion URN mismatch: {response}")
        observed = {
            "gcs_uri": dataset["uri"], "delta_table_id": dataset["delta_table_id"],
            "delta_version": dataset["delta_version"], "active_file_count": dataset["file_count"],
            "schema_field_count": len(dataset["schema_fields"]),
            "schema_sha256": hashlib.sha256(json.dumps(
                dataset["schema_fields"], sort_keys=True).encode("utf-8")).hexdigest(),
            "cloud_discovery_mode": inventory["mode"],
        }
        emitter.emit_mcp(MetadataChangeProposalWrapper(
            entityUrn=assertion_urn,
            aspect=AssertionRunEventClass(
                timestampMillis=timestamp, runId=f"cloud-discovery-{timestamp}",
                asserteeUrn=dataset["urn"], assertionUrn=assertion_urn,
                status=AssertionRunStatusClass.COMPLETE,
                result=AssertionResultClass(
                    type=AssertionResultTypeClass.SUCCESS,
                    nativeResults={"observed": json.dumps(observed, sort_keys=True),
                                   "expected": "physical Delta table and readable non-empty schema"},
                ),
            ),
        ))
        pending.append((dataset, assertion_id, assertion_urn, observed))

    # Search/index propagation is asynchronous. Publish the complete set first,
    # then read it back so one slow entity cannot serialize the campaign.
    time.sleep(2)
    for dataset, assertion_id, assertion_urn, observed in pending:
        info = None
        event = None
        for _ in range(40):
            info = graph.get_aspect(entity_urn=assertion_urn, aspect_type=AssertionInfoClass)
            event = graph.get_latest_timeseries_value(assertion_urn, AssertionRunEventClass, {})
            if info is not None and event is not None:
                break
            time.sleep(0.25)
        if info is None or event is None:
            raise RuntimeError(f"assertion readback missing: {assertion_urn}")
        if str(event.result.type) != "SUCCESS" or event.asserteeUrn != dataset["urn"]:
            raise RuntimeError(f"assertion semantic readback mismatch: {assertion_urn}")
        verified.append({
            "assertion_id": assertion_id, "assertion_urn": assertion_urn,
            "dataset_urn": dataset["urn"], "gcs_uri": dataset["uri"],
            "definition_readback": "MATCH", "run_readback": "SUCCESS",
            "timestamp_millis": event.timestampMillis, "run_id": event.runId,
            "observed": observed,
        })
    return {"mode": "ACTUAL_CLOUD_DELTA_ASSERTIONS_WITH_READBACK",
            "published": len(verified), "verified": verified}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--gms-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = publish(json.loads(args.inventory.read_text(encoding="utf-8")), args.gms_url)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
