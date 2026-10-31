"""Write a local, fail-closed Method 2 stage-pair manifest.

This command performs no cloud access and submits no workload.  It must run
before the two stage submitters so their explicit inputs and immutable outputs
are reviewed as one comparison unit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from data_platform.config.spark_benchmark import StageRunPlan, benchmark_manifest
from data_platform.config.spark_profiles import SPARK_STAGES, resolve_stage_profile


def build_stage_pair_manifest(
    *,
    stage: str,
    dataset_version: str,
    git_sha: str,
    upstream_run_ids: Sequence[str],
    baseline_output_run_id: str,
    baseline_output_root: str,
    candidate_output_run_id: str,
    candidate_output_root: str,
) -> dict[str, object]:
    shared_inputs = tuple(upstream_run_ids)
    plans = (
        StageRunPlan(
            stage=stage,
            profile="baseline",
            upstream_run_ids=shared_inputs,
            output_run_id=baseline_output_run_id,
            output_root=baseline_output_root,
        ),
        StageRunPlan(
            stage=stage,
            profile="optimized",
            upstream_run_ids=shared_inputs,
            output_run_id=candidate_output_run_id,
            output_root=candidate_output_root,
        ),
    )
    properties = {
        profile: dict(
            item.split("=", 1)
            for item in resolve_stage_profile(stage, profile).spark_properties
        )
        for profile in ("baseline", "optimized")
    }
    return benchmark_manifest(
        plans=plans,
        resolved_properties=properties,
        dataset_version=dataset_version,
        git_sha=git_sha,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=SPARK_STAGES, required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--upstream-run-id", action="append", required=True)
    parser.add_argument("--baseline-output-run-id", required=True)
    parser.add_argument("--baseline-output-root", required=True)
    parser.add_argument("--candidate-output-run-id", required=True)
    parser.add_argument("--candidate-output-root", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = build_stage_pair_manifest(
        stage=args.stage,
        dataset_version=args.dataset_version,
        git_sha=args.git_sha,
        upstream_run_ids=args.upstream_run_id,
        baseline_output_run_id=args.baseline_output_run_id,
        baseline_output_root=args.baseline_output_root,
        candidate_output_run_id=args.candidate_output_run_id,
        candidate_output_root=args.candidate_output_root,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
