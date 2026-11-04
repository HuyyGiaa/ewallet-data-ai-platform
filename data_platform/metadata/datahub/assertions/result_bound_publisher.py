"""Publish executed validation outcomes and verify DataHub semantic readback."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .validation_artifact import ValidationArtifact, ValidationOutcome


ASSERTION_TYPE = "E-Wallet Executed Data Quality"
PLATFORM_URN = "urn:li:dataPlatform:gcs"


class Adapter(Protocol):
    def put_definition(self, outcome: ValidationOutcome) -> dict[str, Any]: ...
    def put_run(self, outcome: ValidationOutcome) -> dict[str, Any]: ...
    def get_definition(self, outcome: ValidationOutcome) -> tuple[dict[str, Any], dict[str, Any]]: ...
    def get_run(self, outcome: ValidationOutcome) -> tuple[dict[str, Any], dict[str, Any]]: ...
    def get_run_history_raw(self, outcome: ValidationOutcome) -> dict[str, Any]: ...


def definition(outcome: ValidationOutcome) -> dict[str, Any]:
    return {
        "assertion_urn": outcome.assertion_urn,
        "entity_urn": outcome.dataset_urn,
        "type": ASSERTION_TYPE,
        "description": outcome.description,
        "logic": outcome.rule_name,
        "native_type": outcome.rule_name,
        "native_parameters": {
            "expected": outcome.expected,
            "pipeline": outcome.pipeline,
            "source_git_sha": outcome.source_git_sha,
        },
    }


def run_result(outcome: ValidationOutcome) -> dict[str, Any]:
    if outcome.status not in {"PASS", "FAIL"}:
        raise ValueError(f"{outcome.assertion_id} has no executed result to publish")
    return {
        "assertion_urn": outcome.assertion_urn,
        "assertee_urn": outcome.dataset_urn,
        "run_id": outcome.run_id,
        "timestamp_millis": outcome.timestamp_millis,
        "result_type": "SUCCESS" if outcome.status == "PASS" else "FAILURE",
        "native_results": {
            "observed": json.dumps(outcome.observed, sort_keys=True, default=str),
            "expected": outcome.expected,
            "validator_rule": outcome.rule_name,
            "source_run_id": outcome.source_run_id,
            "source_batch_id": outcome.source_batch_id,
            "source_git_sha": outcome.source_git_sha,
            "source_artifact": outcome.source_artifact,
            "source_artifact_sha256": outcome.source_artifact_sha256,
        },
    }


def _require_match(expected: dict[str, Any], observed: dict[str, Any], label: str) -> None:
    for key, value in expected.items():
        if observed.get(key) != value:
            raise RuntimeError(
                f"{label} readback mismatch for {key}: expected={value!r} observed={observed.get(key)!r}"
            )


def _normalized_run_from_raw(
    raw: dict[str, Any], expected_run_id: str
) -> dict[str, Any] | None:
    """Find a run client-side because GMS 1.7 does not filter this field reliably."""
    for value in raw.get("value", {}).get("values", []):
        encoded = value.get("aspect", {}).get("value")
        if not encoded:
            continue
        event = json.loads(encoded)
        if event.get("runId") != expected_run_id:
            continue
        result = event.get("result") or {}
        return {
            "assertion_urn": event.get("assertionUrn"),
            "assertee_urn": event.get("asserteeUrn"),
            "run_id": event.get("runId"),
            "timestamp_millis": event.get("timestampMillis"),
            "result_type": result.get("type"),
            "native_results": result.get("nativeResults") or {},
        }
    return None


def publish_with_readback(
    artifact: ValidationArtifact,
    adapter: Adapter,
) -> dict[str, Any]:
    publication_started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    verified = []
    definition_only = []
    skipped = []
    for outcome in artifact.outcomes:
        expected_definition = definition(outcome)
        definition_response = adapter.put_definition(outcome)
        observed_definition, raw_definition = adapter.get_definition(outcome)
        _require_match(expected_definition, observed_definition, "assertion definition")
        if outcome.status in {"NOT_EXECUTED", "BLOCKED"}:
            raw_run_history = adapter.get_run_history_raw(outcome)
            run_history_values = raw_run_history.get("value", raw_run_history).get("values", [])
            if run_history_values:
                raise RuntimeError(
                    f"{outcome.assertion_id} is {outcome.status} but DataHub already contains "
                    "assertionRunEvent values"
                )
            definition_only.append({
                "assertion_id": outcome.assertion_id,
                "assertion_urn": outcome.assertion_urn,
                "dataset_urn": outcome.dataset_urn,
                "status": outcome.status,
                "definition_response": definition_response,
                "definition_readback": observed_definition,
                "raw_definition_api_response": raw_definition,
                "raw_run_history_api_response": raw_run_history,
            })
            skipped.append({
                "assertion_id": outcome.assertion_id,
                "status": outcome.status,
                "reason": outcome.observed,
            })
            continue
        expected_run = run_result(outcome)
        run_response = adapter.put_run(outcome)
        observed_run, raw_run = adapter.get_run(outcome)
        _require_match(expected_run, observed_run, "assertion run")
        verified.append({
            "assertion_id": outcome.assertion_id,
            "assertion_urn": outcome.assertion_urn,
            "dataset_urn": outcome.dataset_urn,
            "status": outcome.status,
            "definition_response": definition_response,
            "run_response": run_response,
            "definition_readback": observed_definition,
            "run_readback": observed_run,
            "raw_definition_api_response": raw_definition,
            "raw_run_api_response": raw_run,
        })
    return {
        "mode": "EXECUTED_VALIDATION_ASSERTIONS_WITH_RAW_READBACK",
        "publication_started_at": publication_started_at,
        "publication_completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "published_definition_count": len(verified) + len(definition_only),
        "published_run_event_count": len(verified),
        "published": len(verified),
        "skipped": skipped,
        "definition_only": definition_only,
        "verified": verified,
    }


class DataHubAdapter:
    """DataHub 1.7 adapter; SDK writes plus typed and raw REST readback."""

    def __init__(self, gms_url: str, token: str | None = None) -> None:
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
        from datahub.sdk import DataHubClient

        self.client = DataHubClient(server=gms_url, token=token)
        self.graph = DataHubGraph(DatahubClientConfig(server=gms_url, token=token))
        self.emitter = DatahubRestEmitter(gms_server=gms_url, token=token)
        self.graph.test_connection()

    def put_definition(self, outcome: ValidationOutcome) -> dict[str, Any]:
        expected = definition(outcome)
        response = self.client.assertions.sync_custom_assertion(
            entity_urn=outcome.dataset_urn,
            type=ASSERTION_TYPE,
            description=outcome.description,
            platform_urn=PLATFORM_URN,
            urn=outcome.assertion_urn,
            logic=outcome.rule_name,
            native_type=outcome.rule_name,
            native_parameters=[
                {"key": key, "value": value}
                for key, value in expected["native_parameters"].items()
            ],
        )
        if response.get("urn") != outcome.assertion_urn:
            raise RuntimeError(f"unexpected assertion sync response: {response}")
        return response

    def put_run(self, outcome: ValidationOutcome) -> dict[str, Any]:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata.schema_classes import (
            AssertionResultClass,
            AssertionResultTypeClass,
            AssertionRunEventClass,
            AssertionRunStatusClass,
        )

        expected = run_result(outcome)
        event = AssertionRunEventClass(
            timestampMillis=expected["timestamp_millis"],
            runId=expected["run_id"],
            asserteeUrn=expected["assertee_urn"],
            assertionUrn=expected["assertion_urn"],
            status=AssertionRunStatusClass.COMPLETE,
            result=AssertionResultClass(
                type=(
                    AssertionResultTypeClass.SUCCESS
                    if expected["result_type"] == "SUCCESS"
                    else AssertionResultTypeClass.FAILURE
                ),
                nativeResults=expected["native_results"],
            ),
        )
        self.emitter.emit_mcp(
            MetadataChangeProposalWrapper(entityUrn=outcome.assertion_urn, aspect=event)
        )
        return {"urn": outcome.assertion_urn, "runId": outcome.run_id}

    def get_definition(self, outcome: ValidationOutcome) -> tuple[dict[str, Any], dict[str, Any]]:
        from datahub.metadata.schema_classes import AssertionInfoClass

        info = self.graph.get_aspect(outcome.assertion_urn, AssertionInfoClass)
        if info is None or info.customAssertion is None:
            raise RuntimeError(f"assertionInfo missing: {outcome.assertion_urn}")
        params = dict(info.customAssertion.nativeParameters or {})
        normalized = {
            "assertion_urn": outcome.assertion_urn,
            "entity_urn": info.customAssertion.entity,
            "type": info.customAssertion.type,
            "description": info.description,
            "logic": info.customAssertion.logic,
            "native_type": info.customAssertion.nativeType,
            "native_parameters": params,
        }
        raw = self.graph.get_entity_raw(outcome.assertion_urn, ["assertionInfo"])
        return normalized, raw

    def get_run(self, outcome: ValidationOutcome) -> tuple[dict[str, Any], dict[str, Any]]:
        for _ in range(40):
            raw = self.get_run_history_raw(outcome)
            normalized = _normalized_run_from_raw(raw, outcome.run_id)
            if normalized is not None:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError(f"assertionRunEvent missing: {outcome.run_id}")
        return normalized, raw

    def get_run_history_raw(self, outcome: ValidationOutcome) -> dict[str, Any]:
        query = {
            "urn": outcome.assertion_urn,
            "entity": "assertion",
            "aspect": "assertionRunEvent",
            "limit": 100,
        }
        response = self.graph._session.post(  # SDK session preserves configured auth.
            f"{self.graph._gms_server}/aspects?action=getTimeseriesAspectValues",
            json=query,
        )
        response.raise_for_status()
        return response.json()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--publish", action="store_true")
    parser.add_argument("--gms-url")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    artifact = ValidationArtifact.read(args.validation_artifact)
    if args.dry_run:
        payload = {
            "mode": "DRY_RUN_EXECUTED_VALIDATION_ASSERTIONS",
            "definitions": [definition(item) for item in artifact.outcomes],
            "runs": [
                run_result(item)
                for item in artifact.outcomes
                if item.status in {"PASS", "FAIL"}
            ],
            "not_published": [
                {"assertion_id": item.assertion_id, "status": item.status}
                for item in artifact.outcomes
                if item.status in {"NOT_EXECUTED", "BLOCKED"}
            ],
        }
    else:
        if not args.gms_url:
            raise SystemExit("--gms-url is required with --publish")
        payload = publish_with_readback(
            artifact,
            DataHubAdapter(args.gms_url.rstrip("/"), os.getenv("DATAHUB_GMS_TOKEN")),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{payload['mode']} | output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
