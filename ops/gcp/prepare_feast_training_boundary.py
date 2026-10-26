"""Render the frozen DP3 -> Feast -> bounded training USER_RUN boundary."""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Sequence

from ai_platform.features.feast.cloud_boundary import (
    CANONICAL_TRAINING_SAMPLE_MODULUS,
    CloudFeastTrainingBoundary,
)
from ai_platform.features.feast.provenance import load_feature_selection_evidence


def _command(*parts: str) -> str:
    return shlex.join(parts)


def render_user_commands(
    boundary: CloudFeastTrainingBoundary,
    staging_root: str,
    *,
    feature_selection_evidence: str,
) -> list[str]:
    staging = Path(staging_root) / boundary.snapshot_id
    validation = staging / "validation"
    source = staging / "source"
    dataset = staging / "dataset"
    return [
        f"export EWALLET_FEAST_OFFLINE_DATA_ROOT={shlex.quote(boundary.feast_snapshot_root)}",
        f"export EWALLET_FEAST_REGISTRY={shlex.quote(boundary.feast_registry_uri)}",
        _command(
            "uv", "run", "--project", "ai_platform/features/feast",
            "python", "-m",
            "ai_platform.features.feast.sources.export_gold_to_parquet",
            "--environment", "gcp",
            "--dataset-version", boundary.dataset_version,
            "--source-feature-run-id", boundary.feature_run_id,
            "--snapshot-id", boundary.snapshot_id,
            "--source-feature-root", boundary.feature_gold_root,
            "--output-root", boundary.feast_snapshot_root,
            "--feature-selection-evidence", feature_selection_evidence,
        ),
        _command(
            "uv", "run", "--project", "ai_platform/features/feast", "feast",
            "-c", "ai_platform/features/feast/feature_repo",
            "--feature-store-yaml",
            "ai_platform/features/feast/feature_repo/feature_store.gcp.yaml",
            "apply",
        ),
        _command(
            "uv", "run", "--project", "ai_platform/features/feast",
            "python", "-m",
            "ai_platform.features.feast.sources.export_validation_samples",
            "--environment", "gcp",
            "--silver-root", boundary.silver_root,
            "--feature-gold-root", boundary.feature_gold_root,
            "--output-root", str(validation),
            "--benchmark-rows", "100",
        ),
        _command(
            "uv", "run", "--project", "ai_platform/features/feast",
            "python", "-m",
            "ai_platform.features.feast.validation.canonical_sample",
            "--environment", "gcp",
            "--sample-path", str(validation / "canonical_sample"),
        ),
        _command(
            "uv", "run", "--project", "ai_platform/ml", "python", "-m",
            "ai_platform.ml.training.export_training_source",
            "--environment", "gcp",
            "--silver-root", boundary.silver_root,
            "--dataset-version", boundary.dataset_version,
            "--source-silver-run-id", boundary.silver_run_id,
            "--snapshot-id", boundary.snapshot_id,
            "--output", str(source),
            "--sample-modulus", str(CANONICAL_TRAINING_SAMPLE_MODULUS),
        ),
        _command(
            "uv", "run", "--project", "ai_platform/ml", "python", "-m",
            "ai_platform.ml.training.materialize",
            "--environment", "gcp",
            "--dataset-version", boundary.dataset_version,
            "--source-silver-run-id", boundary.silver_run_id,
            "--source-feature-run-id", boundary.feature_run_id,
            "--snapshot-id", boundary.snapshot_id,
            "--source", str(source),
            "--output", str(dataset),
            "--chunk-size", "1000",
            "--max-new-chunks", "1",
        ),
        _command(
            "uv", "run", "--project", "ai_platform/ml", "python", "-m",
            "ai_platform.ml.training.validate_materialized",
            "--source", str(source), "--dataset", str(dataset),
        ),
        _command("sha256sum", str(dataset / "manifest.json"))
        + " | awk '{print $1}' > "
        + shlex.quote(str(dataset / "manifest.sha256")),
        _command(
            "gcloud", "storage", "ls", f"{boundary.training_snapshot_root}/**",
        ) + "  # must report no matching objects before publish",
        _command(
            "gcloud", "storage", "rsync", "--recursive",
            str(dataset), boundary.training_snapshot_root,
        ),
    ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--silver-run-id", required=True)
    parser.add_argument("--feature-run-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--feature-selection-evidence", required=True)
    parser.add_argument("--staging-root", default="runtime/cloud-training")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    selection = load_feature_selection_evidence(args.feature_selection_evidence)
    expected = (
        args.dataset_version,
        args.silver_run_id,
        args.feature_run_id,
        f"gs://{args.bucket}/lakehouse/runs/{args.feature_run_id}/gold",
    )
    actual = (
        selection.dataset_version,
        selection.silver_run_id,
        selection.feature_run_id,
        selection.feature_root,
    )
    if expected != actual:
        raise ValueError("boundary identifiers do not match selection evidence")
    boundary = CloudFeastTrainingBoundary(
        bucket=args.bucket,
        dataset_version=args.dataset_version,
        silver_run_id=args.silver_run_id,
        feature_run_id=args.feature_run_id,
        snapshot_id=args.snapshot_id,
        source_git_sha=selection.git_sha,
        spark_profile=selection.spark_profile,
        feature_selection_digest=selection.evidence_digest,
    )
    print("DRY RUN — no cloud or data workload was executed")
    print(json.dumps(boundary.as_dict(), indent=2))
    print("\n# Future USER_RUN sequence")
    for command in render_user_commands(
        boundary,
        args.staging_root,
        feature_selection_evidence=args.feature_selection_evidence,
    ):
        print(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
