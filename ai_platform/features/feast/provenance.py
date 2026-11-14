"""Fail-closed provenance for the DP3 output selected for Feast Offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from data_platform.config.dataset_identity import require_approved_dataset
from data_platform.config.spark_profiles import resolve_spark_profile, validate_git_sha


FEATURE_SELECTION_SCHEMA = "feast-feature-selection-v1"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,127}$")
_AMBIGUOUS_IDS = {"active", "current", "latest"}
CANONICAL_DP3_BASIS = "canonical-dp3-pass"
REHEARSAL_DP3_BASIS = "rehearsal-dp3-pass"
_SELECTION_BASES = {CANONICAL_DP3_BASIS, REHEARSAL_DP3_BASIS, "spark-method2-winner-v1"}
DP3_HANDOFF_SCHEMA = "edai1-pipeline-handoff-v1"


def _digest(value: Mapping[str, object]) -> str:
    payload = dict(value)
    payload.pop("evidence_digest", None)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _identifier(value: object, name: str) -> str:
    normalized = _text(value, name)
    if normalized in _AMBIGUOUS_IDS or not _ID.fullmatch(normalized):
        raise ValueError(f"{name} must be an immutable run identifier")
    return normalized


@dataclass(frozen=True)
class FeatureSelectionEvidence:
    dataset_version: str
    silver_run_id: str
    feature_run_id: str
    feature_root: str
    spark_profile: str
    git_sha: str
    selection_basis: str
    source_evidence_digest: str
    evidence_digest: str


def build_feature_selection_evidence(
    *,
    dataset_version: str,
    silver_run_id: str,
    feature_run_id: str,
    feature_root: str,
    spark_profile: str,
    git_sha: str,
    selection_basis: str,
    source_evidence_digest: str,
) -> dict[str, object]:
    """Normalize one verified canonical or Method 2 DP3 selection."""

    profile = resolve_spark_profile(spark_profile).name
    sha = validate_git_sha(git_sha)
    if selection_basis not in _SELECTION_BASES:
        raise ValueError(f"unsupported feature selection basis: {selection_basis!r}")
    if (
        selection_basis == REHEARSAL_DP3_BASIS
        and require_approved_dataset(dataset_version).role != "rehearsal"
    ):
        raise ValueError("rehearsal-dp3-pass applies only to a rehearsal dataset")
    if not _DIGEST.fullmatch(source_evidence_digest):
        raise ValueError("source_evidence_digest must be sha256:<64 lowercase hex>")
    expected_suffix = f"/lakehouse/runs/{feature_run_id}/gold"
    if not feature_root.startswith("gs://") or not feature_root.endswith(
        expected_suffix
    ):
        raise ValueError("source_feature_root must belong to source_feature_run_id")
    value: dict[str, object] = {
        "schema_version": FEATURE_SELECTION_SCHEMA,
        "status": "PASS",
        "dataset_version": _identifier(dataset_version, "dataset_version"),
        "source_silver_run_id": _identifier(silver_run_id, "source_silver_run_id"),
        "source_feature_run_id": _identifier(feature_run_id, "source_feature_run_id"),
        "source_feature_root": feature_root,
        "spark_profile": profile,
        "source_git_sha": sha,
        "selection_basis": selection_basis,
        "source_evidence_digest": source_evidence_digest,
    }
    value["evidence_digest"] = _digest(value)
    return value


def load_feature_selection_evidence(path: str | Path) -> FeatureSelectionEvidence:
    """Load a normalized selection and reject stale or edited evidence."""

    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("feature selection evidence must be a JSON object")
    if value.get("schema_version") != FEATURE_SELECTION_SCHEMA:
        raise ValueError("feature selection evidence schema mismatch")
    if value.get("status") != "PASS":
        raise ValueError("feature selection evidence must have PASS status")
    identity = value.get("evidence_digest")
    if identity != _digest(value):
        raise ValueError("feature selection evidence digest mismatch")
    source_digest = _text(
        value.get("source_evidence_digest"), "source_evidence_digest"
    )
    if not _DIGEST.fullmatch(source_digest):
        raise ValueError("source_evidence_digest must be sha256:<64 lowercase hex>")
    normalized = build_feature_selection_evidence(
        dataset_version=_text(value.get("dataset_version"), "dataset_version"),
        silver_run_id=_text(
            value.get("source_silver_run_id"), "source_silver_run_id"
        ),
        feature_run_id=_text(
            value.get("source_feature_run_id"), "source_feature_run_id"
        ),
        feature_root=_text(value.get("source_feature_root"), "source_feature_root"),
        spark_profile=_text(value.get("spark_profile"), "spark_profile"),
        git_sha=_text(value.get("source_git_sha"), "source_git_sha"),
        selection_basis=_text(value.get("selection_basis"), "selection_basis"),
        source_evidence_digest=source_digest,
    )
    if normalized["evidence_digest"] != identity:
        raise ValueError("feature selection evidence normalization mismatch")
    return FeatureSelectionEvidence(
        dataset_version=str(normalized["dataset_version"]),
        silver_run_id=str(normalized["source_silver_run_id"]),
        feature_run_id=str(normalized["source_feature_run_id"]),
        feature_root=str(normalized["source_feature_root"]),
        spark_profile=str(normalized["spark_profile"]),
        git_sha=str(normalized["source_git_sha"]),
        selection_basis=str(normalized["selection_basis"]),
        source_evidence_digest=source_digest,
        evidence_digest=str(identity),
    )


def _run_id_from_root(root: object, layer: str) -> str:
    text = _text(root, f"{layer} root")
    match = re.fullmatch(rf"gs://[^/]+/lakehouse/runs/([^/]+)/{layer}", text)
    if match is None:
        raise ValueError(f"not a run-scoped {layer} root: {text}")
    return match.group(1)


def selection_from_dp3_completion(raw: bytes) -> dict[str, object]:
    """Derive the Feast selection from one DP3 completion handoff.

    The handoff is the manifest the DP3 DAG writes after the features stage and its
    validation succeed. Only a ``PASS`` validation of a ``SUCCEEDED`` batch whose
    output root belongs to the recorded feature run, in the dataset's own bucket, is
    accepted. The digest of the exact handoff bytes becomes the source evidence.
    """

    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != DP3_HANDOFF_SCHEMA:
        raise ValueError("not a DP3 completion handoff")
    if value.get("pipeline") != "dp3":
        raise ValueError("handoff is not from the DP3 pipeline")
    dataset_version = _text(value.get("dataset_version"), "dataset_version")
    identity = require_approved_dataset(dataset_version)
    stage = value.get("stages", {}).get("features", {})
    metadata = stage.get("execution_metadata", {})
    if stage.get("validation", {}).get("status") != "PASS":
        raise ValueError("DP3 features validation is not PASS")
    if metadata.get("dataproc_batch_state") != "SUCCEEDED":
        raise ValueError("DP3 features batch did not succeed")
    feature_root = _text(stage.get("output_root"), "output_root")
    feature_run_id = _run_id_from_root(feature_root, "gold")
    if feature_run_id != stage.get("run_id") or feature_run_id != value.get(
        "run_ids", {}
    ).get("features"):
        raise ValueError("DP3 output root does not belong to the recorded feature run")
    bucket = feature_root.removeprefix("gs://").split("/", 1)[0]
    if bucket != value.get("data_bucket") or bucket != identity.data_bucket:
        raise ValueError("DP3 output is not in the dataset's approved bucket")
    silver = [
        root for root in metadata.get("input_roots", []) if str(root).endswith("/silver")
    ]
    if len(silver) != 1:
        raise ValueError("DP3 handoff must name exactly one Silver input root")
    basis = REHEARSAL_DP3_BASIS if identity.role == "rehearsal" else CANONICAL_DP3_BASIS
    return build_feature_selection_evidence(
        dataset_version=dataset_version,
        silver_run_id=_run_id_from_root(silver[0], "silver"),
        feature_run_id=feature_run_id,
        feature_root=feature_root,
        spark_profile=_text(value.get("spark_profile"), "spark_profile"),
        git_sha=_text(value.get("git_sha"), "git_sha"),
        selection_basis=basis,
        source_evidence_digest="sha256:" + hashlib.sha256(raw).hexdigest(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Write the selection evidence for one local copy of a DP3 completion handoff."""

    parser = argparse.ArgumentParser(description=selection_from_dp3_completion.__doc__)
    parser.add_argument("--dp3-completion", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    evidence = selection_from_dp3_completion(args.dp3_completion.read_bytes())
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    load_feature_selection_evidence(args.output)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
