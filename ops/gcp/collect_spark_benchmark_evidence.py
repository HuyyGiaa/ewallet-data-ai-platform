"""Collect read-only Managed Spark batch evidence after one benchmark stage."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from data_platform.config.spark_profiles import (
    require_profile_binding,
    resolve_spark_profile,
    validate_benchmark_id,
    validate_git_sha,
)


_STAGE_RUN_KIND = {
    "dp1-bronze": "bronze",
    "dp2-silver": "silver",
    "dp2-gold": "gold",
    "dp3-features": "feat",
}


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_batch_evidence(
    description: Mapping[str, Any],
    *,
    benchmark_id: str,
    spark_profile: str,
    stage: str,
    dataset_version: str,
    git_sha: str,
    upstream_run_ids: Sequence[str],
    output_run_ids: Sequence[str],
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    validate_benchmark_id(benchmark_id)
    profile = resolve_spark_profile(spark_profile).name
    git_sha = validate_git_sha(git_sha)
    usage = description.get("approximateUsage", {})
    runtime = description.get("runtimeConfig", {})
    if not isinstance(usage, Mapping) or not isinstance(runtime, Mapping):
        raise ValueError("batch description is missing runtime/usage objects")
    create_time = str(description["createTime"])
    state_time = str(description["stateTime"])
    milli_dcu_seconds = float(usage.get("milliDcuSeconds", 0))
    properties = runtime.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError("runtimeConfig.properties must be an object")
    batch_id = str(description.get("name", "")).rsplit("/", 1)[-1]
    try:
        run_kind = _STAGE_RUN_KIND[stage]
    except KeyError as exc:
        raise ValueError(f"unsupported benchmark evidence stage: {stage}") from exc
    require_profile_binding(
        batch_id,
        benchmark_id=benchmark_id,
        profile=profile,
        stage=run_kind,
    )
    labels = description.get("labels", {})
    if not isinstance(labels, Mapping):
        raise ValueError("batch labels must be an object")
    expected_labels = {
        "benchmark_id": benchmark_id,
        "spark_profile": profile,
        "git_sha": git_sha,
    }
    for key, expected in expected_labels.items():
        if labels.get(key) != expected:
            raise ValueError(f"batch label {key} does not match evidence metadata")
    if runtime.get("version") != "2.2":
        raise ValueError("benchmark batch runtime must be 2.2")
    resolved = resolve_spark_profile(profile)
    expected_properties = {
        "dataproc.tier": "standard",
        **dict(item.split("=", 1) for item in resolved.spark_properties),
    }
    for key, expected in expected_properties.items():
        if str(properties.get(key)) != expected:
            raise ValueError(
                f"batch runtime property {key} is not {expected!r}"
            )
    return {
        "benchmark_id": benchmark_id,
        "spark_profile": profile,
        "stage": stage,
        "git_sha": git_sha,
        "dataset_version": dataset_version,
        "upstream_run_ids": list(upstream_run_ids),
        "output_run_ids": list(output_run_ids),
        "batch_id": batch_id,
        "batch_state": description.get("state"),
        "create_time": create_time,
        "state_time": state_time,
        "wall_clock_seconds": (_timestamp(state_time) - _timestamp(create_time)).total_seconds(),
        "runtime_version": runtime.get("version"),
        "runtime_properties": dict(sorted(properties.items())),
        "milli_dcu_seconds": milli_dcu_seconds,
        "dcu_hours": milli_dcu_seconds / 3_600_000,
        "shuffle_storage_gb_seconds": float(usage.get("shuffleStorageGbSeconds", 0)),
        "driver_output_uri": description.get("runtimeInfo", {}).get(
            "outputUri"
        ),
        "validation": dict(validation),
    }


def collect(
    *,
    project: str,
    region: str,
    batch_id: str,
    runner: Callable[..., Any] = subprocess.run,
    **metadata: Any,
) -> dict[str, Any]:
    completed = runner(
        [
            "gcloud", "dataproc", "batches", "describe", batch_id,
            f"--project={project}", f"--region={region}", "--format=json",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    description = json.loads(completed.stdout)
    return parse_batch_evidence(description, **metadata)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True); parser.add_argument("--region", required=True)
    parser.add_argument("--batch-id", required=True); parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--spark-profile", choices=("baseline", "optimized"), required=True)
    parser.add_argument("--stage", required=True); parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--git-sha", required=True); parser.add_argument("--upstream-run-id", action="append", default=[])
    parser.add_argument("--output-run-id", action="append", default=[])
    parser.add_argument("--validation-json", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = collect(
        project=args.project, region=args.region, batch_id=args.batch_id,
        benchmark_id=args.benchmark_id, spark_profile=args.spark_profile,
        stage=args.stage, dataset_version=args.dataset_version, git_sha=args.git_sha,
        upstream_run_ids=args.upstream_run_id, output_run_ids=args.output_run_id,
        validation=json.loads(args.validation_json.read_text(encoding="utf-8")),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
