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
from ops.gcp.publish_training_snapshot import (
    PUBLICATION_AUTHORIZATION as TRAINING_SNAPSHOT_AUTHORIZATION,
    TrustedRegistryIdentity,
    require_pit_pass_evidence,
)


RECOVERED_SNAPSHOT_WORKFLOW = "recovered-snapshot"
NEW_SNAPSHOT_WORKFLOW = "new-snapshot"
NEW_SNAPSHOT_AUTHORIZATION = "AUTHORIZE_NEW_IMMUTABLE_FEAST_SNAPSHOT"
PIT_PHASE = "pit"
TRAINING_PHASE = "training"


def _command(*parts: str) -> str:
    return shlex.join(parts)


def _registry_command(
    boundary: CloudFeastTrainingBoundary,
    *,
    evidence_output: str,
    mode: str,
) -> str:
    command = _command(
        "uv", "run", "--locked", "--no-sync", "--project",
        "ai_platform/features/feast", "python", "-m",
        "ops.gcp.publish_feast_registry",
        "--dataset-version", boundary.dataset_version,
        "--silver-run-id", boundary.silver_run_id,
        "--feature-run-id", boundary.feature_run_id,
        "--snapshot-id", boundary.resolved_feast_snapshot_id,
        "--source-git-sha", boundary.source_git_sha,
        "--feature-selection-digest", boundary.feature_selection_digest,
        "--feature-store-config",
        "ai_platform/features/feast/feature_repo/feature_store.gcp.yaml",
    )
    command += (
        ' --project-id "$GCP_PROJECT_ID"'
        ' --data-bucket "$EWALLET_DATA_BUCKET"'
        ' --expected-branch "$FEAST_EXPECTED_BRANCH"'
        ' --expected-execution-git-sha "$PIT_EXECUTION_GIT_SHA"'
        ' --expected-manifest-sha256 "$FEAST_OFFLINE_MANIFEST_SHA256"'
        ' --expected-manifest-generation "$FEAST_OFFLINE_MANIFEST_GENERATION"'
        f' --evidence-output "${evidence_output}"'
    )
    if mode == "readback-only":
        return command + (
            ' readback-only'
            ' --trusted-registry-artifact "$FEAST_REGISTRY_ARTIFACT"'
        )
    if mode == "plan":
        return command + " plan"
    if mode == "publish":
        return command + (
            " publish"
            " --authorization CREATE_ONLY_FEAST_REGISTRY"
            ' --registry-artifact-output "$FEAST_REGISTRY_ARTIFACT_OUTPUT"'
        )
    raise ValueError(f"unsupported registry operation mode: {mode}")


def render_user_commands(
    boundary: CloudFeastTrainingBoundary,
    staging_root: str,
    *,
    feature_selection_evidence: str,
    workflow: str = RECOVERED_SNAPSHOT_WORKFLOW,
    new_snapshot_authorization: str | None = None,
    phase: str = PIT_PHASE,
    pit_evidence: Path | None = None,
    registry_identity: TrustedRegistryIdentity | None = None,
    training_snapshot_authorization: str | None = None,
) -> list[str]:
    if workflow not in {RECOVERED_SNAPSHOT_WORKFLOW, NEW_SNAPSHOT_WORKFLOW}:
        raise ValueError(f"unsupported Feast training workflow: {workflow}")
    if workflow == NEW_SNAPSHOT_WORKFLOW:
        if new_snapshot_authorization != NEW_SNAPSHOT_AUTHORIZATION:
            raise ValueError(
                "new-snapshot workflow requires explicit authorization "
                f"{NEW_SNAPSHOT_AUTHORIZATION!r}"
            )
    elif new_snapshot_authorization is not None:
        raise ValueError(
            "new-snapshot authorization is invalid for recovered-snapshot workflow"
        )
    if phase not in {PIT_PHASE, TRAINING_PHASE}:
        raise ValueError(f"unsupported Feast training phase: {phase}")
    if phase == TRAINING_PHASE:
        if training_snapshot_authorization != TRAINING_SNAPSHOT_AUTHORIZATION:
            raise ValueError(
                "training phase requires explicit authorization "
                f"{TRAINING_SNAPSHOT_AUTHORIZATION!r}"
            )
        if pit_evidence is None:
            raise ValueError("training phase requires PIT PASS evidence")
        if registry_identity is None:
            raise ValueError("training phase requires trusted Registry identity")
        require_pit_pass_evidence(
            pit_evidence, boundary, registry_identity=registry_identity
        )
    elif (
        pit_evidence is not None
        or registry_identity is not None
        or training_snapshot_authorization is not None
    ):
        raise ValueError("training-only arguments are invalid for PIT phase")

    staging = Path(staging_root) / boundary.snapshot_id
    validation = staging / "validation"
    source = staging / "source"
    dataset = staging / "dataset"
    submit_validation = _command(
        "python", "-m", "ops.gcp.submit_feast_training_export",
        "--job", "validation-samples",
        "--dataset-version", boundary.dataset_version,
        "--silver-run-id", boundary.silver_run_id,
        "--feature-run-id", boundary.feature_run_id,
        "--snapshot-id", boundary.snapshot_id,
        "--feast-snapshot-id", boundary.resolved_feast_snapshot_id,
        "--feature-selection-evidence", feature_selection_evidence,
        "--ttl", "8h",
    ) + ' --batch-id "$FEAST_VALIDATION_BATCH_ID" --execute'
    submit_source = _command(
        "python", "-m", "ops.gcp.submit_feast_training_export",
        "--job", "training-source",
        "--dataset-version", boundary.dataset_version,
        "--silver-run-id", boundary.silver_run_id,
        "--feature-run-id", boundary.feature_run_id,
        "--snapshot-id", boundary.snapshot_id,
        "--feast-snapshot-id", boundary.resolved_feast_snapshot_id,
        "--feature-selection-evidence", feature_selection_evidence,
        "--sample-modulus", str(CANONICAL_TRAINING_SAMPLE_MODULUS),
        "--ttl", "8h",
    ) + ' --batch-id "$TRAINING_SOURCE_BATCH_ID" --execute'
    materialize = _command(
        "uv", "run", "--locked", "--project",
        "ai_platform/features/feast", "python", "-m",
        "ai_platform.ml.training.materialize",
        "--environment", "gcp",
        "--dataset-version", boundary.dataset_version,
        "--source-silver-run-id", boundary.silver_run_id,
        "--source-feature-run-id", boundary.feature_run_id,
        "--snapshot-id", boundary.snapshot_id,
        "--source-git-sha", boundary.source_git_sha,
        "--feature-selection-digest", boundary.feature_selection_digest,
        "--source", str(source),
        "--output", str(dataset),
        "--chunk-size", "1000",
        "--max-new-chunks", "1",
    )
    pit_validate = _command(
        "uv", "run", "--locked", "--project", "ai_platform/features/feast",
        "python", "-m",
        "ai_platform.features.feast.validation.canonical_sample",
        "--environment", "gcp",
        "--sample-path", str(validation / "canonical_sample"),
        "--expected-dataset-version", boundary.dataset_version,
        "--expected-source-silver-run-id", boundary.silver_run_id,
        "--expected-source-feature-run-id", boundary.feature_run_id,
        "--expected-source-silver-root", boundary.silver_root,
        "--expected-source-feature-root", boundary.feature_gold_root,
        "--expected-snapshot-id", boundary.resolved_feast_snapshot_id,
        "--expected-source-git-sha", boundary.source_git_sha,
        "--expected-feature-selection-digest", boundary.feature_selection_digest,
    ) + (
        ' --registry-readback-evidence "$FEAST_REGISTRY_READBACK_EVIDENCE"'
        ' --expected-registry-generation "$FEAST_REGISTRY_GENERATION"'
        ' --expected-registry-sha256 "$FEAST_REGISTRY_SHA256"'
        ' --expected-execution-git-sha "$PIT_EXECUTION_GIT_SHA"'
        ' --evidence-output "$PIT_VALIDATION_EVIDENCE"'
    )
    environment = [
        'export GCP_PROJECT_ID="<gcp-project-id>"',
        f"export EWALLET_DATA_BUCKET={shlex.quote(boundary.bucket)}",
        f"export EWALLET_SILVER_ROOT={shlex.quote(boundary.silver_root)}",
        f"export EWALLET_FEATURE_GOLD_ROOT={shlex.quote(boundary.feature_gold_root)}",
        "export EWALLET_VALIDATION_STAGING_ROOT="
        f"{shlex.quote(boundary.validation_staging_root)}",
        "export EWALLET_TRAINING_SOURCE_STAGING_ROOT="
        f"{shlex.quote(boundary.training_source_staging_root)}",
        f"export EWALLET_FEAST_OFFLINE_DATA_ROOT={shlex.quote(boundary.feast_snapshot_root)}",
        f"export EWALLET_FEAST_REGISTRY={shlex.quote(boundary.feast_registry_uri)}",
        'export PIT_EXECUTION_GIT_SHA="$(git rev-parse HEAD)"',
        'export FEAST_EXPECTED_BRANCH="<reviewed-branch>"',
        'export FEAST_OFFLINE_MANIFEST_SHA256="<verified-manifest-sha256>"',
        'export FEAST_OFFLINE_MANIFEST_GENERATION="<verified-manifest-generation>"',
        'export FEAST_REGISTRY_ARTIFACT="<absolute-trusted-registry-artifact.db>"',
        'export FEAST_REGISTRY_READBACK_EVIDENCE="<absolute-readback-evidence.json>"',
        'export FEAST_REGISTRY_GENERATION="<verified-generation>"',
        'export FEAST_REGISTRY_SHA256="<verified-sha256>"',
        'export PIT_VALIDATION_EVIDENCE="<absolute-new-pit-evidence.json>"',
    ]
    if workflow == RECOVERED_SNAPSHOT_WORKFLOW:
        preparation = [
            _registry_command(
                boundary,
                evidence_output="FEAST_REGISTRY_READBACK_EVIDENCE",
                mode="readback-only",
            )
        ]
    else:
        preparation = [
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
            'export FEAST_REGISTRY_PLAN_EVIDENCE="<absolute-new-plan-evidence.json>"',
            _registry_command(
                boundary,
                evidence_output="FEAST_REGISTRY_PLAN_EVIDENCE",
                mode="plan",
            ),
            'export FEAST_REGISTRY_PUBLISH_EVIDENCE="<absolute-new-publish-evidence.json>"',
            'export FEAST_REGISTRY_ARTIFACT_OUTPUT="<absolute-new-registry-artifact.db>"',
            _registry_command(
                boundary,
                evidence_output="FEAST_REGISTRY_PUBLISH_EVIDENCE",
                mode="publish",
            ),
            'export FEAST_REGISTRY_ARTIFACT="$FEAST_REGISTRY_ARTIFACT_OUTPUT"',
            _registry_command(
                boundary,
                evidence_output="FEAST_REGISTRY_READBACK_EVIDENCE",
                mode="readback-only",
            ),
        ]
    pit_commands = [
        "export FEAST_VALIDATION_BATCH_ID=feast-val-$(date -u +%y%m%d%H%M%S)",
        submit_validation,
        'gcloud dataproc batches describe "$FEAST_VALIDATION_BATCH_ID" '
        f"--region {shlex.quote('asia-southeast1')} --format=json",
        'test "$(gcloud dataproc batches describe "$FEAST_VALIDATION_BATCH_ID" '
        '--region asia-southeast1 --format="value(state)")" = SUCCEEDED',
        _command("test", "!", "-e", str(validation)),
        _command(
            "gcloud", "storage", "rsync", "--recursive",
            boundary.validation_staging_root, str(validation),
        ),
        pit_validate,
    ]
    if phase == PIT_PHASE:
        return environment + preparation + pit_commands

    assert pit_evidence is not None
    assert registry_identity is not None
    registry_arguments = _command(
        "--trusted-registry-readback-evidence",
        str(registry_identity.readback_evidence),
        "--expected-registry-uri",
        registry_identity.uri,
        "--expected-registry-generation",
        str(registry_identity.generation),
        "--expected-registry-sha256",
        registry_identity.sha256,
        "--expected-registry-execution-git-sha",
        registry_identity.execution_git_sha,
    )
    submit_source += (
        f" --pit-evidence {shlex.quote(str(pit_evidence))} {registry_arguments}"
    )
    training_publication = _command(
        "uv", "run", "--locked", "--no-sync", "--project",
        "ai_platform/features/feast", "python", "-m",
        "ops.gcp.publish_training_snapshot",
        "--data-bucket", boundary.bucket,
        "--dataset-version", boundary.dataset_version,
        "--silver-run-id", boundary.silver_run_id,
        "--feature-run-id", boundary.feature_run_id,
        "--snapshot-id", boundary.snapshot_id,
        "--feast-snapshot-id", boundary.resolved_feast_snapshot_id,
        "--source-git-sha", boundary.source_git_sha,
        "--feature-selection-digest", boundary.feature_selection_digest,
        "--source", str(dataset),
        "--pit-evidence", str(pit_evidence),
        "--trusted-registry-readback-evidence",
        str(registry_identity.readback_evidence),
        "--expected-registry-uri", registry_identity.uri,
        "--expected-registry-generation", str(registry_identity.generation),
        "--expected-registry-sha256", registry_identity.sha256,
        "--expected-registry-execution-git-sha",
        registry_identity.execution_git_sha,
        "--authorization", TRAINING_SNAPSHOT_AUTHORIZATION,
    ) + (
        ' --project-id "$GCP_PROJECT_ID"'
        ' --execution-git-sha "$ML1_EXECUTION_GIT_SHA"'
        ' --evidence-output "$TRAINING_SNAPSHOT_PUBLICATION_EVIDENCE"'
    )
    return environment + [
        'export ML1_EXECUTION_GIT_SHA="$(git rev-parse HEAD)"',
        f"export VERIFIED_PIT_EVIDENCE={shlex.quote(str(pit_evidence))}",
        "export TRAINING_SNAPSHOT_PUBLICATION_EVIDENCE="
        '"<absolute-new-training-publication-evidence.json>"',
        "export TRAINING_SOURCE_BATCH_ID=train-src-$(date -u +%y%m%d%H%M%S)",
        submit_source,
        'gcloud dataproc batches describe "$TRAINING_SOURCE_BATCH_ID" '
        f"--region {shlex.quote('asia-southeast1')} --format=json",
        'test "$(gcloud dataproc batches describe "$TRAINING_SOURCE_BATCH_ID" '
        '--region asia-southeast1 --format="value(state)")" = SUCCEEDED',
        _command("test", "!", "-e", str(source)),
        _command(
            "gcloud", "storage", "rsync", "--recursive",
            boundary.training_source_staging_root, str(source),
        ),
        materialize,
        "while test -f "
        + shlex.quote(str(dataset / "_INCOMPLETE"))
        + "; do "
        + materialize
        + " --resume; done",
        _command(
            "uv", "run", "--project", "ai_platform/ml", "python", "-m",
            "ai_platform.ml.training.validate_materialized",
            "--source", str(source), "--dataset", str(dataset),
        ),
        _command("sha256sum", str(dataset / "manifest.json"))
        + " | awk '{print $1}' > "
        + shlex.quote(str(dataset / "manifest.sha256")),
        training_publication,
    ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--silver-run-id", required=True)
    parser.add_argument("--feature-run-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--feast-snapshot-id", required=True)
    parser.add_argument("--feature-selection-evidence", required=True)
    parser.add_argument("--staging-root", default="runtime/cloud-training")
    parser.add_argument(
        "--workflow",
        choices=(RECOVERED_SNAPSHOT_WORKFLOW, NEW_SNAPSHOT_WORKFLOW),
        default=RECOVERED_SNAPSHOT_WORKFLOW,
        help=(
            "recovered-snapshot is readback/PIT only; new-snapshot enables the "
            "separate create-only snapshot workflow"
        ),
    )
    parser.add_argument(
        "--new-snapshot-authorization",
        help=f"required only for new snapshots; must equal {NEW_SNAPSHOT_AUTHORIZATION}",
    )
    parser.add_argument(
        "--phase",
        choices=(PIT_PHASE, TRAINING_PHASE),
        default=PIT_PHASE,
    )
    parser.add_argument("--pit-evidence", type=Path)
    parser.add_argument("--trusted-registry-readback-evidence", type=Path)
    parser.add_argument("--expected-registry-uri")
    parser.add_argument("--expected-registry-generation", type=int)
    parser.add_argument("--expected-registry-sha256")
    parser.add_argument("--expected-registry-execution-git-sha")
    parser.add_argument(
        "--training-snapshot-authorization",
        help=(
            "required only for the training phase; must equal "
            f"{TRAINING_SNAPSHOT_AUTHORIZATION}"
        ),
    )
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
        feast_snapshot_id=args.feast_snapshot_id,
    )
    identity_values = (
        args.trusted_registry_readback_evidence,
        args.expected_registry_uri,
        args.expected_registry_generation,
        args.expected_registry_sha256,
        args.expected_registry_execution_git_sha,
    )
    if any(value is not None for value in identity_values) and not all(
        value is not None for value in identity_values
    ):
        raise ValueError("trusted Registry identity arguments must be supplied together")
    registry_identity = None
    if all(value is not None for value in identity_values):
        registry_identity = TrustedRegistryIdentity(
            readback_evidence=args.trusted_registry_readback_evidence,
            uri=args.expected_registry_uri,
            generation=args.expected_registry_generation,
            sha256=args.expected_registry_sha256,
            execution_git_sha=args.expected_registry_execution_git_sha,
        )
    print("DRY RUN — no cloud or data workload was executed")
    print(json.dumps(boundary.as_dict(), indent=2))
    print("\n# Future USER_RUN sequence")
    for command in render_user_commands(
        boundary,
        args.staging_root,
        feature_selection_evidence=args.feature_selection_evidence,
        workflow=args.workflow,
        new_snapshot_authorization=args.new_snapshot_authorization,
        phase=args.phase,
        pit_evidence=args.pit_evidence,
        registry_identity=registry_identity,
        training_snapshot_authorization=args.training_snapshot_authorization,
    ):
        print(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
