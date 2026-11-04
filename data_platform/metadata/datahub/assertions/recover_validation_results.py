"""Recover genuine DP1/DP2/DP3 validation results from persisted evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .validation_artifact import ValidationArtifact, ValidationOutcome, utc_now


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _events(path: Path) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        result.append((str(item.get("timestamp", "")), str(item.get("event", ""))))
    return result


def _embedded_object(events: list[tuple[str, str]], schema_version: str) -> dict[str, Any]:
    values = [event for _, event in events]
    for start in range(len(values)):
        if values[start].strip() != "{":
            continue
        for end in range(start + 1, len(values)):
            if values[end].strip() != "}":
                continue
            try:
                payload = json.loads("\n".join(values[start : end + 1]))
            except json.JSONDecodeError:
                continue
            if payload.get("schema_version") == schema_version:
                return payload
    raise ValueError(f"no embedded {schema_version} artifact found")


def _inventory_lookup(inventory: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    if inventory.get("mode") != "READ_ONLY_REAL_CLOUD_DISCOVERY":
        raise ValueError("dataset inventory must be verified cloud discovery")
    return {(item["zone"], item["table"]): item for item in inventory["datasets"]}


def recover_dp1(path: Path, datasets: dict[tuple[str, str], dict[str, Any]]) -> ValidationOutcome:
    payload = _embedded_object(_events(path), "r4-source-to-bronze-v1")
    tables = payload["tables"]
    mismatches = {
        name: {"source_rows": item["source_rows"], "bronze_rows": item["bronze_rows"]}
        for name, item in tables.items()
        if item["source_rows"] != item["bronze_rows"]
    }
    target = datasets[("bronze", "transactions")]
    status = "PASS" if payload.get("status") == "PASS" and not mismatches else "FAIL"
    return ValidationOutcome(
        assertion_id="dq_dp1_bronze_source_row_reconciliation",
        pipeline="dp1_source_to_bronze",
        dataset_urn=target["urn"],
        dataset_uri=target["uri"],
        rule_name="source_to_bronze.row_reconciliation",
        description="Every persisted Bronze table preserves its source row population.",
        status=status,
        expected="source_rows = bronze_rows for all 8 DP1 tables",
        observed={"compared_tables": len(tables), "mismatch_count": len(mismatches), "mismatches": mismatches},
        executed_at=payload["finished_at"],
        source_run_id=payload["run_id"],
        source_batch_id=payload["batch_id"],
        source_git_sha=payload["git_sha"],
        source_artifact=str(path),
        source_artifact_sha256=_sha256(path),
    )


def _metric(events: list[tuple[str, str]], label: str) -> tuple[int, str]:
    pattern = re.compile(rf"{re.escape(label)}\s*:\s*([0-9,]+)")
    for timestamp, event in events:
        match = pattern.search(event)
        if match:
            return int(match.group(1).replace(",", "")), timestamp
    raise ValueError(f"missing executed metric: {label}")


def recover_dp2(path: Path, datasets: dict[tuple[str, str], dict[str, Any]]) -> tuple[ValidationOutcome, ...]:
    events = _events(path)
    nulls, null_timestamp = _metric(events, "Silver channel NULL")
    duplicates, duplicate_timestamp = _metric(events, "Silver duplicated tx IDs")
    if not any("Transaction DQ validation: PASSED" in event for _, event in events):
        raise ValueError("DP2 log has metrics but no completed transaction DQ gate")
    run_match = re.search(r"lakehouse/runs/([^/]+)/silver/transactions", "\n".join(event for _, event in events))
    if run_match is None:
        raise ValueError("cannot recover DP2 Silver run identity")
    run_id = run_match.group(1)
    git_match = re.search(r"--git-sha '([0-9a-f]{40})'", "\n".join(event for _, event in events))
    target = datasets[("silver", "transactions")]
    common = {
        "pipeline": "dp2_bronze_to_curated",
        "dataset_urn": target["urn"],
        "dataset_uri": target["uri"],
        "source_run_id": run_id,
        "source_batch_id": run_id,
        "source_git_sha": git_match.group(1) if git_match else "",
        "source_artifact": str(path),
        "source_artifact_sha256": _sha256(path),
    }
    return (
        ValidationOutcome(
            assertion_id="dq_dp2_silver_transactions_channel_not_null",
            rule_name="transactions.channel_not_null",
            description="Silver transformation leaves no transaction row with a NULL channel.",
            status="PASS" if nulls == 0 else "FAIL",
            expected="0 NULL channel rows",
            observed={"invalid_count": nulls},
            executed_at=null_timestamp,
            **common,
        ),
        ValidationOutcome(
            assertion_id="dq_dp2_silver_transactions_unique_id",
            rule_name="transactions.duplicate_key",
            description="Silver transformation leaves no duplicate transaction_id group.",
            status="PASS" if duplicates == 0 else "FAIL",
            expected="0 duplicate transaction_id groups",
            observed={"duplicate_group_count": duplicates},
            executed_at=duplicate_timestamp,
            **common,
        ),
    )


def recover_dp3(
    pit_path: Path,
    execution_path: Path,
    datasets: dict[tuple[str, str], dict[str, Any]],
) -> tuple[ValidationOutcome, ...]:
    payload = json.loads(pit_path.read_text(encoding="utf-8"))
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    completed = execution["status"]["conditions"][0]
    if completed.get("type") != "Completed" or completed.get("status") != "True":
        raise ValueError("DP3 PIT execution did not complete successfully")
    executed_at = execution["status"]["completionTime"]
    lineage = payload["validation_export_lineage"]
    run_id = lineage["source_feature_run_id"]
    execution_id = execution["metadata"]["name"]
    overall_pass = (
        payload.get("status") == "PASS"
        and payload.get("mismatch_count") == 0
        and payload.get("comparable_values") == payload.get("expected_comparable_values")
        and payload.get("labels_passed_to_feast") is False
    )
    outcomes = []
    for view in payload["feature_view_retrievals"]:
        table = f"feat_{view['view_name']}"
        target = datasets[("feature", table)]
        outcomes.append(ValidationOutcome(
            assertion_id=f"dq_dp3_{table}_pit_readback",
            pipeline="dp3_historical_features",
            dataset_urn=target["urn"],
            dataset_uri=target["uri"],
            rule_name=f"{table}.point_in_time_readback",
            description=(
                f"{table} participates in the executed Feast point-in-time readback with "
                "no mismatched comparable feature values and no label leakage."
            ),
            status="PASS" if overall_pass else "FAIL",
            expected="310/310 comparable values match, mismatch_count=0, labels excluded",
            observed={
                "overall_comparable_values": payload["comparable_values"],
                "overall_expected_comparable_values": payload["expected_comparable_values"],
                "overall_mismatch_count": payload["mismatch_count"],
                "labels_passed_to_feast": payload["labels_passed_to_feast"],
                "retrieved_rows": view["retrieved_rows"],
                "timestamp_field": view["timestamp_field"],
                "source_path": view["source_path"],
                "cloud_run_execution": execution_id,
            },
            executed_at=executed_at,
            source_run_id=run_id,
            source_batch_id=execution_id,
            source_git_sha=lineage["source_git_sha"],
            source_artifact=str(pit_path),
            source_artifact_sha256=_sha256(pit_path),
        ))
    return tuple(outcomes)


def dp2_gold_not_executed(
    datasets: dict[tuple[str, str], dict[str, Any]],
) -> ValidationOutcome:
    """Record the audited Gold row-level gap without manufacturing a run."""
    target = datasets[("gold", "fact_transactions")]
    return ValidationOutcome(
        assertion_id="dq_dp2_gold_fact_integrity",
        pipeline="dp2_bronze_to_curated",
        dataset_urn=target["urn"],
        dataset_uri=target["uri"],
        rule_name="fact_transactions.row_level_integrity",
        description="Gold fact row-level integrity gate reserved for an actual validator result.",
        status="NOT_EXECUTED",
        expected="required keys non-null, unique transaction_id, valid dimension references",
        observed={
            "reason": (
                "Historical evidence proves job/table-set success but contains no persisted "
                "row-level Gold validator result. No SUCCESS run may be published."
            )
        },
        executed_at="",
        source_run_id="",
        source_batch_id="",
        source_git_sha="",
        source_artifact="",
        source_artifact_sha256="",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--dp1-airflow-log", type=Path, required=True)
    parser.add_argument("--dp2-airflow-log", type=Path, required=True)
    parser.add_argument("--dp3-pit-json", type=Path, required=True)
    parser.add_argument("--dp3-execution-json", type=Path, required=True)
    parser.add_argument(
        "--generated-at",
        help=(
            "Explicit artifact generation timestamp for byte-identical historical "
            "reproduction; defaults to the current UTC time for new recoveries"
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    datasets = _inventory_lookup(inventory)
    outcomes = (
        recover_dp1(args.dp1_airflow_log, datasets),
        *recover_dp2(args.dp2_airflow_log, datasets),
        dp2_gold_not_executed(datasets),
        *recover_dp3(args.dp3_pit_json, args.dp3_execution_json, datasets),
    )
    ValidationArtifact(
        outcomes=outcomes,
        generated_at=args.generated_at or utc_now(),
    ).write(args.output)
    print(f"RECOVERED | {len(outcomes)} genuine validation outcomes | output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
