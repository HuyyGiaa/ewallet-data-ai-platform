"""Extract original Spark application metrics from downloaded event logs.

The event log belongs to the completed ingestion batch, never to the later
read-only semantic-summary batch. Inputs are local, operator-downloaded files;
the command performs no cloud reads or writes and creates its JSON output once.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

from data_platform.config.spark_benchmark import (
    METHOD2_OUTPUT_RUN_PROPERTY,
    METHOD2_PAIR_DIGEST_PROPERTY,
    ORIGINAL_PERFORMANCE_SCHEMA,
    original_performance_digest,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_text(path: Path) -> IO[str]:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def _events(paths: Sequence[Path]) -> Iterable[Mapping[str, Any]]:
    if not paths:
        raise ValueError("at least one Spark event-log file is required")
    for path in paths:
        if not path.is_file():
            raise ValueError(f"Spark event-log file does not exist: {path}")
        with _open_text(path) as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"malformed Spark event JSON at {path}:{line_number}"
                    ) from exc
                if not isinstance(value, Mapping):
                    raise ValueError(
                        f"Spark event must be an object at {path}:{line_number}"
                    )
                yield value


def summarize_event_logs(
    paths: Sequence[Path],
    *,
    batch_id: str,
    output_run_id: str,
    pair_manifest_digest: str,
    source_uri: str,
) -> dict[str, object]:
    """Return measured metrics bound to one completed ingestion execution."""

    if not batch_id or batch_id != output_run_id:
        raise ValueError("original batch_id must equal output_run_id")
    if not pair_manifest_digest.startswith("sha256:"):
        raise ValueError("pair_manifest_digest must be an explicit SHA-256 identity")
    if not source_uri.startswith("gs://"):
        raise ValueError("source_uri must identify the original GCS event log")

    app_name: str | None = None
    app_id: str | None = None
    started_ms: int | None = None
    ended_ms: int | None = None
    spark_properties: dict[str, str] | None = None
    task_count = 0
    failed_task_count = 0
    memory_spill_bytes = 0
    disk_spill_bytes = 0
    completed_jobs = 0
    completed_stages = 0

    for event in _events(paths):
        event_type = event.get("Event")
        if event_type == "SparkListenerApplicationStart":
            if started_ms is not None:
                raise ValueError("Spark event log contains multiple application starts")
            app_name = str(event.get("App Name", ""))
            app_id = str(event.get("App ID", "")) or None
            started_ms = int(event["Timestamp"])
        elif event_type == "SparkListenerApplicationEnd":
            if ended_ms is not None:
                raise ValueError("Spark event log contains multiple application ends")
            ended_ms = int(event["Timestamp"])
        elif event_type == "SparkListenerEnvironmentUpdate":
            raw_properties = event.get("Spark Properties")
            if isinstance(raw_properties, Mapping):
                current = {str(key): str(value) for key, value in raw_properties.items()}
                if spark_properties is not None and current != spark_properties:
                    raise ValueError("Spark properties change across event-log segments")
                spark_properties = current
        elif event_type == "SparkListenerTaskEnd":
            task_count += 1
            metrics = event.get("Task Metrics", {})
            if not isinstance(metrics, Mapping):
                raise ValueError("SparkListenerTaskEnd is missing Task Metrics")
            memory_spill_bytes += int(metrics.get("Memory Bytes Spilled", 0))
            disk_spill_bytes += int(metrics.get("Disk Bytes Spilled", 0))
            reason = event.get("Task End Reason")
            if isinstance(reason, Mapping):
                failed_task_count += int(reason.get("Reason") != "Success")
            else:
                failed_task_count += int(reason != "Success")
        elif event_type == "SparkListenerJobEnd":
            completed_jobs += 1
        elif event_type == "SparkListenerStageCompleted":
            completed_stages += 1

    if started_ms is None or ended_ms is None or ended_ms <= started_ms:
        raise ValueError("Spark event log lacks a valid application start/end interval")
    if not app_name:
        raise ValueError("Spark event log lacks application name")
    if not app_id:
        raise ValueError("Spark event log lacks application ID")
    if spark_properties is None:
        raise ValueError("Spark event log lacks Spark Properties")
    if spark_properties.get("spark.app.id") != app_id:
        raise ValueError("Spark application ID mismatch")
    expected_properties = {
        METHOD2_PAIR_DIGEST_PROPERTY: pair_manifest_digest,
        METHOD2_OUTPUT_RUN_PROPERTY: output_run_id,
    }
    for key, expected in expected_properties.items():
        if spark_properties.get(key) != expected:
            raise ValueError(f"Spark event-log property mismatch: {key}")
    if task_count <= 0:
        raise ValueError("Spark event log contains no completed task attempts")
    if completed_jobs <= 0 or completed_stages <= 0:
        raise ValueError("Spark event log lacks completed job/stage evidence")

    result: dict[str, object] = {
        "schema_version": ORIGINAL_PERFORMANCE_SCHEMA,
        "source_execution_kind": "original_ingestion_batch",
        "batch_id": batch_id,
        "output_run_id": output_run_id,
        "pair_manifest_digest": pair_manifest_digest,
        "event_log_source_uri": source_uri,
        "application_id": app_id,
        "application_name": app_name,
        "application_start_epoch_ms": started_ms,
        "application_end_epoch_ms": ended_ms,
        "application_runtime_seconds": (ended_ms - started_ms) / 1000,
        "task_count": task_count,
        "failed_task_count": failed_task_count,
        "memory_spill_bytes": memory_spill_bytes,
        "disk_spill_bytes": disk_spill_bytes,
        "spill_bytes": memory_spill_bytes + disk_spill_bytes,
        "completed_job_count": completed_jobs,
        "completed_stage_count": completed_stages,
        "event_log_files": [
            {
                "path": str(path),
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
            for path in paths
        ],
    }
    result["original_performance_digest"] = original_performance_digest(result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-log", action="append", type=Path, required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--output-run-id", required=True)
    parser.add_argument("--pair-manifest-digest", required=True)
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = summarize_event_logs(
        args.event_log,
        batch_id=args.batch_id,
        output_run_id=args.output_run_id,
        pair_manifest_digest=args.pair_manifest_digest,
        source_uri=args.source_uri,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
