"""Collect the execution provenance of campaign Spark batches (read-only).

Two sources, neither inferred: Dataproc batch metadata (state, times, runtime,
history endpoints) and the record each job writes at startup with its real Spark
application ID and event-log directory. Any value that is not available is null;
nothing is guessed from a batch ID. A record that names another campaign, run or
bucket raises ``ProvenanceError`` instead of being reported.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from data_platform.config.dataset_identity import resolve_storage_context


TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
_FRACTION = re.compile(r"(\.\d{6})\d*")
HISTORY_ENDPOINT_KEYS = ("Spark History Server", "spark_history_server")


class ProvenanceError(ValueError):
    """Execution metadata belongs to another campaign, run or bucket."""


@dataclass(frozen=True)
class ExecutionContext:
    """What this execution must be, from the DagRun parameters."""

    campaign_id: str
    dataset_version: str
    spark_profile: str
    stage_name: str
    kind: str  # transformation or validation
    git_sha: str
    source_manifest_sha256: str
    project_id: str
    region: str
    data_bucket: str
    run_id: str
    input_roots: tuple[str, ...]
    output_roots: tuple[str, ...]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = _FRACTION.sub(r"\1", value.replace("Z", "+00:00"))
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def describe_batch(context: ExecutionContext, batch_id: str, runner: Callable[..., Any]) -> Mapping[str, Any] | None:
    """Return the batch metadata, or None when Dataproc has no such batch."""

    completed = runner(
        ["gcloud", "dataproc", "batches", "describe", batch_id, f"--project={context.project_id}",
         f"--region={context.region}", "--format=json"],
        check=False, text=True, capture_output=True,
    )
    if completed.returncode == 0:
        return json.loads(completed.stdout)
    if "not_found" in (completed.stdout + completed.stderr).lower():
        return None
    raise ProvenanceError(f"could not read batch {batch_id}: {completed.stderr.strip()}")


def _read_json(uri: str, runner: Callable[..., Any]) -> Mapping[str, Any] | None:
    completed = runner(["gcloud", "storage", "cat", uri], check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        return None
    value = json.loads(completed.stdout)
    return value if isinstance(value, Mapping) else None


def _existing_event_log(directory: str, application_id: str, runner: Callable[..., Any]) -> str | None:
    """Spark names the log after the application; report it only if it exists."""

    completed = runner(["gcloud", "storage", "ls", f"{directory.rstrip('/')}/{application_id}*"],
                       check=False, text=True, capture_output=True)
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return lines[0] if completed.returncode == 0 and lines else None


def _check_identity(context: ExecutionContext, batch_id: str, batch: Mapping[str, Any] | None,
                    record: Mapping[str, Any] | None) -> None:
    if context.kind == "transformation":
        mapped = batch_id == context.run_id
    else:
        mapped = re.fullmatch(re.escape(context.run_id) + r"-v[1-9]", batch_id) is not None
    if not mapped:
        raise ProvenanceError(f"batch {batch_id} does not map to run {context.run_id}")
    if batch is not None:
        labels = batch.get("labels") or {}
        for key, expected in (("benchmark_id", context.campaign_id.rsplit("-", 1)[0]),
                              ("spark_profile", context.spark_profile), ("git_sha", context.git_sha)):
            if labels.get(key) != expected:
                raise ProvenanceError(f"batch {batch_id} label {key} is {labels.get(key)!r}, expected {expected!r}")
    if record is not None:
        for key, expected in (("run_id", context.run_id), ("dataset_version", context.dataset_version)):
            if record.get(key) != expected:
                raise ProvenanceError(f"execution record {key} is {record.get(key)!r}, expected {expected!r}")
        directory = record.get("spark_event_log_dir")
        if directory:
            bucket = urlsplit(str(directory)).netloc
            if bucket != context.data_bucket:
                raise ProvenanceError(f"event log bucket {bucket} is not the campaign bucket {context.data_bucket}")


def collect_execution(
    context: ExecutionContext,
    *,
    batch_id: str,
    record_uri: str,
    report_uri: str | None,
    runner: Callable[..., Any],
) -> dict[str, Any]:
    """Return one execution record. Repeated collection of a finished batch is identical."""

    batch = describe_batch(context, batch_id, runner)
    record = _read_json(record_uri, runner)
    _check_identity(context, batch_id, batch, record)
    report = _read_json(report_uri, runner) if report_uri else None
    state = batch.get("state") if batch else None
    history = (batch or {}).get("stateHistory") or []
    start = next((item.get("stateStartTime") for item in history if item.get("state") == "RUNNING"), None) \
        or (batch or {}).get("createTime")
    end = (batch or {}).get("stateTime") if state in TERMINAL_STATES else None
    started, ended = _parse_time(start), _parse_time(end)
    application_id = record.get("spark_application_id") if record else None
    log_dir = record.get("spark_event_log_dir") if record else None
    endpoints = (batch or {}).get("runtimeInfo", {}).get("endpoints") or {}
    return {
        "campaign_id": context.campaign_id,
        "dataset_version": context.dataset_version,
        "spark_profile": context.spark_profile,
        "stage_name": context.stage_name,
        "kind": context.kind,
        "git_sha": context.git_sha,
        "source_manifest_sha256": context.source_manifest_sha256,
        "dataproc_batch_id": batch_id,
        "dataproc_batch_found": batch is not None,
        "dataproc_batch_state": state,
        "spark_application_id": application_id,
        "spark_event_log_dir": log_dir,
        "spark_event_log_uri": _existing_event_log(log_dir, application_id, runner) if log_dir and application_id else None,
        "spark_execution_record_uri": record_uri if record else None,
        "history_server_uri": next((endpoints[key] for key in HISTORY_ENDPOINT_KEYS if key in endpoints), None),
        "dataproc_endpoints": dict(endpoints),
        "driver_output_uri": ((batch or {}).get("runtimeInfo") or {}).get("outputUri"),
        "project_id": context.project_id,
        "region": context.region,
        "runtime_version": ((batch or {}).get("runtimeConfig") or {}).get("version"),
        "job_start_timestamp": start,
        "job_end_timestamp": end,
        "job_duration_seconds": (ended - started).total_seconds() if started and ended else None,
        "input_roots": list(context.input_roots),
        "output_roots": list(context.output_roots),
        "validation_report_uri": report_uri if report else None,
        "validation_status": report.get("status") if report else None,
    }


def campaign_id(benchmark_id: str, spark_profile: str) -> str:
    return f"{benchmark_id}-{spark_profile}"


def stage_inputs(context, stage: str, runs: Mapping[str, str]) -> tuple[str, ...]:
    """Roots a transformation of ``stage`` reads."""

    return {
        "bronze": (f"{context.source_root}/_control/dataset_manifest.json",),
        "silver": (context.run_root(runs["bronze"], "bronze"),),
        "gold": (context.run_root(runs["silver"], "silver"),),
        "features": (context.run_root(runs["silver"], "silver"), context.run_root(runs["gold"], "gold")),
    }[stage]


def stage_output(context, stage: str, runs: Mapping[str, str]) -> str:
    zone = {"bronze": "bronze", "silver": "silver", "gold": "gold", "features": "gold"}[stage]
    return context.run_root(runs[stage], zone)


def validation_inputs(context, stage: str, runs: Mapping[str, str]) -> tuple[str, ...]:
    """Roots the stage validator reads."""

    return {
        "bronze": (context.run_root(runs["bronze"], "bronze"),),
        "silver": (context.run_root(runs["bronze"], "bronze"), context.run_root(runs["silver"], "silver")),
        "gold": (context.run_root(runs["silver"], "silver"), context.run_root(runs["gold"], "gold")),
        "features": (context.run_root(runs["silver"], "silver"), context.run_root(runs["gold"], "gold"),
                     context.run_root(runs["features"], "gold")),
    }[stage]


def collect_campaign(args: argparse.Namespace, runner: Callable[..., Any]) -> dict[str, Any]:
    """Provenance of every transformation and validator attempt found, at any time."""

    from ops.gcp.submit_source_to_bronze import execution_record_uri
    from ops.gcp.submit_stage_validation import report_uri, validation_batch_id

    storage = resolve_storage_context(args.dataset_version, args.data_bucket)
    runs = {"bronze": args.bronze_run_id, "silver": args.silver_run_id,
            "gold": args.gold_run_id, "features": args.features_run_id}
    identity = {
        "campaign_id": campaign_id(args.benchmark_id, args.spark_profile),
        "dataset_version": args.dataset_version, "spark_profile": args.spark_profile, "git_sha": args.git_sha,
        "source_manifest_sha256": args.source_manifest_sha256, "project_id": args.project_id,
        "region": args.region, "data_bucket": args.data_bucket,
    }
    scope = {"bronze": "bronze", "silver": "silver", "gold": "core-gold", "features": "features"}
    stages: dict[str, Any] = {}
    for stage, run_id in runs.items():
        transformation = ExecutionContext(stage_name=stage, kind="transformation", run_id=run_id,
                                          input_roots=stage_inputs(storage, stage, runs),
                                          output_roots=(stage_output(storage, stage, runs),), **identity)
        stages[stage] = {"execution_metadata": collect_execution(
            transformation, batch_id=run_id, record_uri=execution_record_uri(args.data_bucket, run_id, run_id),
            report_uri=None, runner=runner)}
        validator = ExecutionContext(stage_name=stage, kind="validation", run_id=run_id,
                                     input_roots=validation_inputs(storage, stage, runs), output_roots=(), **identity)
        attempts = []
        for attempt in range(1, 10):
            batch = validation_batch_id(run_id, attempt)
            if describe_batch(validator, batch, runner) is None:
                break
            attempts.append(collect_execution(
                validator, batch_id=batch,
                record_uri=execution_record_uri(args.data_bucket, run_id, batch),
                report_uri=report_uri(args.data_bucket, run_id, scope[stage], attempt), runner=runner))
        stages[stage]["validation_attempts"] = attempts
    return {"campaign_id": identity["campaign_id"], "stages": stages}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dataset-version", "benchmark-id", "spark-profile", "git-sha", "source-manifest-sha256",
                 "bronze-run-id", "silver-run-id", "gold-run-id", "features-run-id",
                 "project-id", "region", "data-bucket"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    import subprocess

    args = parse_args(argv)
    try:
        result = collect_campaign(args, subprocess.run)
    except ProvenanceError as error:
        print(f"PROVENANCE REFUSED: {error}", file=sys.stderr)
        return 1
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
