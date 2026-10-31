"""Fail-closed Method 2 stage-pair identity and evidence contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from data_platform.config.spark_profiles import (
    parse_benchmark_run_id,
    resolve_spark_profile,
    resolve_spark_stage,
    resolve_stage_profile,
    validate_benchmark_id,
    validate_git_sha,
)


PAIR_MANIFEST_SCHEMA = "spark-method2-pair-v2"
EQUIVALENCE_SCHEMA = "spark-method2-equivalence-v2"
WINNER_SCHEMA = "spark-method2-winner-v1"
SEMANTIC_SUMMARY_SCHEMA = "spark-method2-semantic-summary-v1"
ORIGINAL_PERFORMANCE_SCHEMA = "spark-method2-original-performance-v1"
VALIDATION_EVIDENCE_SCHEMA = "spark-method2-validation-evidence-v1"
METHOD2_PAIR_DIGEST_PROPERTY = "spark.ewallet.method2.pairManifestDigest"
METHOD2_OUTPUT_RUN_PROPERTY = "spark.ewallet.method2.outputRunId"

_OUTPUT_STAGE = {
    "dp1-bronze": "bronze",
    "dp2-silver": "silver",
    "dp2-gold": "gold",
    "dp3-features": "feat",
}
_OUTPUT_LAYER = {
    "dp1-bronze": "bronze",
    "dp2-silver": "silver",
    "dp2-gold": "gold",
    "dp3-features": "gold",
}
_UPSTREAM_CARDINALITY = {
    "dp1-bronze": 1,
    "dp2-silver": 1,
    "dp2-gold": 1,
    "dp3-features": 2,
}


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def normalize_spark_properties(value: Mapping[str, object]) -> dict[str, str]:
    """Normalize without dropping keys or accepting nested/malformed values."""

    if not isinstance(value, Mapping) or not value:
        raise ValueError("spark_properties must be a non-empty object")
    normalized: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key or isinstance(raw, (dict, list, tuple)):
            raise ValueError("spark_properties contains a malformed property")
        normalized[key] = str(raw)
    return dict(sorted(normalized.items()))


def spark_properties_digest(value: Mapping[str, object]) -> str:
    return _canonical_digest(normalize_spark_properties(value))


def pair_manifest_digest(value: Mapping[str, object]) -> str:
    payload = dict(value)
    payload.pop("pair_manifest_digest", None)
    return _canonical_digest(payload)


def equivalence_artifact_digest(value: Mapping[str, object]) -> str:
    payload = dict(value)
    payload.pop("equivalence_artifact_digest", None)
    return _canonical_digest(payload)


def winner_artifact_digest(value: Mapping[str, object]) -> str:
    payload = dict(value)
    payload.pop("winner_artifact_digest", None)
    return _canonical_digest(payload)


def semantic_summary_digest(value: Mapping[str, object]) -> str:
    payload = dict(value)
    payload.pop("semantic_summary_digest", None)
    return _canonical_digest(payload)


def original_performance_digest(value: Mapping[str, object]) -> str:
    payload = dict(value)
    payload.pop("original_performance_digest", None)
    return _canonical_digest(payload)


def validation_evidence_digest(value: Mapping[str, object]) -> str:
    payload = dict(value)
    payload.pop("validation_evidence_digest", None)
    return _canonical_digest(payload)


def expected_spark_properties(stage: str, profile: str) -> dict[str, str]:
    resolved = resolve_stage_profile(stage, profile)
    return normalize_spark_properties(
        dict(item.split("=", 1) for item in resolved.spark_properties)
    )


def is_managed_spark_22(version: object) -> bool:
    value = str(version)
    return value == "2.2" or value.startswith("2.2.")


@dataclass(frozen=True)
class StageRunPlan:
    stage: str
    profile: str
    upstream_run_ids: tuple[str, ...]
    output_run_id: str
    output_root: str

    def __post_init__(self) -> None:
        resolve_spark_stage(self.stage)
        resolve_spark_profile(self.profile)
        output_identity = parse_benchmark_run_id(self.output_run_id)
        if output_identity["spark_profile"] != self.profile:
            raise ValueError("benchmark output profile does not match plan profile")
        if output_identity["stage"] != _OUTPUT_STAGE[self.stage]:
            raise ValueError("benchmark output stage does not match plan stage")
        if not self.upstream_run_ids or any(not value for value in self.upstream_run_ids):
            raise ValueError("benchmark upstream run IDs must be explicit")
        if len(self.upstream_run_ids) != _UPSTREAM_CARDINALITY[self.stage]:
            raise ValueError("benchmark upstream lineage has wrong stage cardinality")
        if not self.output_run_id or self.output_run_id in self.upstream_run_ids:
            raise ValueError("benchmark output must be new and immutable")
        if "/lakehouse/runs/" not in self.output_root:
            raise ValueError("benchmark output root must be run-scoped")
        if f"/lakehouse/runs/{self.output_run_id}/" not in self.output_root:
            raise ValueError("benchmark output root must belong to output_run_id")
        if not self.output_root.rstrip("/").endswith(f"/{_OUTPUT_LAYER[self.stage]}"):
            raise ValueError("benchmark output root has the wrong stage layer")


def require_common_stage_inputs(
    baseline: StageRunPlan, candidate: StageRunPlan
) -> None:
    if baseline.stage != candidate.stage:
        raise ValueError("benchmark pair must target the same stage")
    if baseline.profile != "baseline" or candidate.profile != "optimized":
        raise ValueError("benchmark pair must contain baseline then optimized")
    if (
        parse_benchmark_run_id(baseline.output_run_id)["benchmark_id"]
        != parse_benchmark_run_id(candidate.output_run_id)["benchmark_id"]
    ):
        raise ValueError("benchmark pair must share one benchmark ID")
    if baseline.upstream_run_ids != candidate.upstream_run_ids:
        raise ValueError("Method 2 requires identical upstream run IDs")
    if baseline.output_run_id == candidate.output_run_id:
        raise ValueError("baseline and candidate outputs must be isolated")
    if baseline.output_root == candidate.output_root:
        raise ValueError("baseline and candidate output roots must be isolated")


REQUIRED_RUNTIME_METRICS = (
    "application_runtime_seconds",
    "batch_runtime_seconds",
    "dcu_hours",
    "shuffle_storage_gb_seconds",
    "spill_bytes",
    "task_count",
    "output_file_count",
    "output_total_bytes",
)


def benchmark_manifest(
    *,
    plans: Sequence[StageRunPlan],
    resolved_properties: Mapping[str, Mapping[str, str]],
    dataset_version: str,
    git_sha: str,
) -> dict[str, object]:
    if len(plans) != 2:
        raise ValueError("a Method 2 stage benchmark requires exactly two plans")
    require_common_stage_inputs(plans[0], plans[1])
    benchmark_id = parse_benchmark_run_id(plans[0].output_run_id)["benchmark_id"]
    validate_benchmark_id(benchmark_id)
    validate_git_sha(git_sha)
    if not dataset_version:
        raise ValueError("dataset_version must be explicit")
    runs = []
    for plan in plans:
        properties = normalize_spark_properties(resolved_properties[plan.profile])
        runs.append(
            {
                "spark_profile": plan.profile,
                "output_run_id": plan.output_run_id,
                "output_root": plan.output_root.rstrip("/"),
                "spark_properties": properties,
                "spark_properties_digest": spark_properties_digest(properties),
            }
        )
    manifest: dict[str, object] = {
        "schema_version": PAIR_MANIFEST_SCHEMA,
        "method": "stage-common-input",
        "benchmark_id": benchmark_id,
        "stage": plans[0].stage,
        "dataset_version": dataset_version,
        "git_sha": git_sha,
        "upstream_run_ids": list(plans[0].upstream_run_ids),
        "runs": runs,
        "required_runtime_metrics": list(REQUIRED_RUNTIME_METRICS),
    }
    manifest["pair_manifest_digest"] = pair_manifest_digest(manifest)
    return validate_pair_manifest(manifest)


def validate_pair_manifest(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("pair manifest must be an object")
    manifest = dict(value)
    if manifest.get("schema_version") != PAIR_MANIFEST_SCHEMA:
        raise ValueError("pair manifest schema_version is missing or unsupported")
    if manifest.get("method") != "stage-common-input":
        raise ValueError("pair manifest method must be stage-common-input")
    expected_digest = manifest.get("pair_manifest_digest")
    if not isinstance(expected_digest, str) or not expected_digest:
        raise ValueError("pair_manifest_digest is required")
    if expected_digest != pair_manifest_digest(manifest):
        raise ValueError("pair manifest digest mismatch")
    benchmark_id = validate_benchmark_id(str(manifest.get("benchmark_id", "")))
    stage = resolve_spark_stage(str(manifest.get("stage", "")))
    dataset_version = manifest.get("dataset_version")
    if not isinstance(dataset_version, str) or not dataset_version:
        raise ValueError("pair manifest dataset_version is required")
    git_sha = validate_git_sha(str(manifest.get("git_sha", "")))
    upstream = manifest.get("upstream_run_ids")
    if not isinstance(upstream, list) or any(
        not isinstance(item, str) or not item for item in upstream
    ):
        raise ValueError("pair manifest upstream_run_ids must be explicit")
    if len(upstream) != _UPSTREAM_CARDINALITY[stage]:
        raise ValueError("pair manifest upstream_run_ids have wrong stage cardinality")
    if stage == "dp1-bronze" and (
        dataset_version != "canonical-100gb-v1" or upstream != [dataset_version]
    ):
        raise ValueError("DP1 Method 2 must bind canonical-100gb-v1")
    if manifest.get("required_runtime_metrics") != list(REQUIRED_RUNTIME_METRICS):
        raise ValueError("pair manifest required_runtime_metrics mismatch")
    runs = manifest.get("runs")
    if not isinstance(runs, list) or len(runs) != 2:
        raise ValueError("pair manifest must contain exactly two runs")
    normalized_runs: list[dict[str, object]] = []
    for expected_role, run in zip(("baseline", "optimized"), runs):
        if not isinstance(run, Mapping):
            raise ValueError("pair manifest run must be an object")
        if run.get("spark_profile") != expected_role:
            raise ValueError(
                "pair manifest spark_profile roles must be baseline/optimized"
            )
        plan = StageRunPlan(
            stage=stage,
            profile=expected_role,
            upstream_run_ids=tuple(upstream),
            output_run_id=str(run.get("output_run_id", "")),
            output_root=str(run.get("output_root", "")),
        )
        if plan.output_root != plan.output_root.rstrip("/"):
            raise ValueError("pair manifest output_root must not have a trailing slash")
        if parse_benchmark_run_id(plan.output_run_id)["benchmark_id"] != benchmark_id:
            raise ValueError("pair manifest output_run_id has different benchmark_id")
        properties = normalize_spark_properties(run.get("spark_properties", {}))
        if properties != expected_spark_properties(stage, expected_role):
            raise ValueError(
                f"pair manifest {expected_role} spark_properties mismatch"
            )
        digest = run.get("spark_properties_digest")
        if digest != spark_properties_digest(properties):
            raise ValueError(
                f"pair manifest {expected_role} spark_properties_digest mismatch"
            )
        normalized_runs.append(
            {
                "spark_profile": expected_role,
                "output_run_id": plan.output_run_id,
                "output_root": plan.output_root.rstrip("/"),
                "spark_properties": properties,
                "spark_properties_digest": digest,
            }
        )
    if normalized_runs[0]["output_run_id"] == normalized_runs[1]["output_run_id"]:
        raise ValueError("pair manifest output_run_ids must be isolated")
    if normalized_runs[0]["output_root"] == normalized_runs[1]["output_root"]:
        raise ValueError("pair manifest output roots must be isolated")
    manifest.update(
        benchmark_id=benchmark_id,
        stage=stage,
        dataset_version=dataset_version,
        git_sha=git_sha,
        upstream_run_ids=list(upstream),
        runs=normalized_runs,
    )
    return manifest


def load_pair_manifest(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("pair manifest JSON must contain an object")
    return validate_pair_manifest(value)


def pair_member(
    manifest: Mapping[str, object], spark_profile: str
) -> dict[str, object]:
    checked = validate_pair_manifest(manifest)
    profile = resolve_spark_profile(spark_profile).name
    for run in checked["runs"]:
        if run["spark_profile"] == profile:
            return dict(run)
    raise ValueError(f"pair manifest has no spark_profile={profile}")


def validate_pair_member(
    manifest: Mapping[str, object],
    *,
    spark_profile: str,
    benchmark_id: str,
    stage: str,
    dataset_version: str,
    git_sha: str,
    upstream_run_ids: Sequence[str],
    output_run_id: str,
    output_root: str,
) -> dict[str, object]:
    checked = validate_pair_manifest(manifest)
    supplied = {
        "benchmark_id": benchmark_id,
        "stage": stage,
        "dataset_version": dataset_version,
        "git_sha": git_sha,
        "upstream_run_ids": list(upstream_run_ids),
    }
    for field, actual in supplied.items():
        if checked[field] != actual:
            raise ValueError(f"pair manifest {field} mismatch")
    member = pair_member(checked, spark_profile)
    if not output_run_id or member["output_run_id"] != output_run_id:
        raise ValueError("pair manifest output_run_id mismatch")
    if not output_root or member["output_root"] != output_root.rstrip("/"):
        raise ValueError("pair manifest output_root mismatch")
    return member


def method2_runtime_properties(
    manifest: Mapping[str, object], spark_profile: str
) -> dict[str, str]:
    checked = validate_pair_manifest(manifest)
    member = pair_member(checked, spark_profile)
    return {
        METHOD2_PAIR_DIGEST_PROPERTY: str(checked["pair_manifest_digest"]),
        METHOD2_OUTPUT_RUN_PROPERTY: str(member["output_run_id"]),
    }


def validate_runtime_profile_properties(
    manifest: Mapping[str, object],
    *,
    spark_profile: str,
    runtime_properties: Mapping[str, object],
) -> dict[str, str]:
    """Validate the complete stage/profile-controlled Spark property namespace."""

    checked = validate_pair_manifest(manifest)
    member = pair_member(checked, spark_profile)
    actual = normalize_spark_properties(runtime_properties)
    controlled_keys = set(expected_spark_properties(checked["stage"], "baseline"))
    controlled_keys.update(
        expected_spark_properties(checked["stage"], "optimized")
    )
    expected = member["spark_properties"]
    for key in controlled_keys:
        if key in expected:
            if actual.get(key) != expected[key]:
                raise ValueError(f"runtime Spark property mismatch: {key}")
        elif key in actual:
            raise ValueError(
                f"runtime Spark property conflicts with {spark_profile}: {key}"
            )
    return normalize_spark_properties(expected)


def validate_stage_evidence(
    manifest: Mapping[str, object],
    evidence: Mapping[str, object],
    *,
    expected_profile: str,
) -> dict[str, object]:
    checked = validate_pair_manifest(manifest)
    if not isinstance(evidence, Mapping):
        raise ValueError("stage evidence must be an object")
    member = pair_member(checked, expected_profile)
    fields = {
        "pair_manifest_digest": checked["pair_manifest_digest"],
        "benchmark_id": checked["benchmark_id"],
        "stage": checked["stage"],
        "dataset_version": checked["dataset_version"],
        "git_sha": checked["git_sha"],
        "spark_profile": expected_profile,
        "output_run_id": member["output_run_id"],
        "output_root": member["output_root"],
        "upstream_run_ids": checked["upstream_run_ids"],
        "spark_properties_digest": member["spark_properties_digest"],
    }
    for field, expected in fields.items():
        if evidence.get(field) != expected:
            raise ValueError(f"stage evidence {field} mismatch")
    properties = evidence.get("spark_properties")
    if not isinstance(properties, Mapping):
        raise ValueError("stage evidence spark_properties is required")
    if normalize_spark_properties(properties) != member["spark_properties"]:
        raise ValueError("stage evidence spark_properties mismatch")
    return dict(evidence)


def output_identity(
    manifest: Mapping[str, object], profile: str
) -> dict[str, object]:
    checked = validate_pair_manifest(manifest)
    member = pair_member(checked, profile)
    return {
        "spark_profile": profile,
        "output_run_id": member["output_run_id"],
        "output_root": member["output_root"],
        "spark_properties_digest": member["spark_properties_digest"],
    }


def validate_equivalence_evidence(
    manifest: Mapping[str, object],
    evidence: Mapping[str, object],
) -> dict[str, object]:
    checked = validate_pair_manifest(manifest)
    if evidence.get("high_confidence_equivalence") is not True:
        raise ValueError(
            "unequal or missing semantic equivalence cannot produce a winner"
        )
    expected = {
        "schema_version": EQUIVALENCE_SCHEMA,
        "pair_manifest_digest": checked["pair_manifest_digest"],
        "benchmark_id": checked["benchmark_id"],
        "dataset_version": checked["dataset_version"],
        "git_sha": checked["git_sha"],
        "stage": checked["stage"],
        "source_run_ids": checked["upstream_run_ids"],
        "compared_outputs": {
            "baseline": output_identity(checked, "baseline"),
            "optimized": output_identity(checked, "optimized"),
        },
    }
    for field, expected_value in expected.items():
        if evidence.get(field) != expected_value:
            raise ValueError(f"equivalence evidence {field} mismatch")
    identity = evidence.get("equivalence_artifact_digest")
    if identity != equivalence_artifact_digest(evidence):
        raise ValueError("equivalence artifact identity mismatch")
    return dict(evidence)


def select_stage_winner(
    *,
    pair_manifest: Mapping[str, object],
    baseline_evidence: Mapping[str, object],
    candidate_evidence: Mapping[str, object],
    equivalence_evidence: Mapping[str, object],
) -> dict[str, object]:
    """Select only from the two exact outputs bound by one validated pair."""

    checked = validate_pair_manifest(pair_manifest)
    baseline = validate_stage_evidence(
        checked, baseline_evidence, expected_profile="baseline"
    )
    optimized = validate_stage_evidence(
        checked, candidate_evidence, expected_profile="optimized"
    )
    for label, evidence in (("baseline", baseline), ("optimized", optimized)):
        if (
            evidence.get("status") != "PASS"
            or evidence.get("batch_state") != "SUCCEEDED"
        ):
            raise ValueError(f"{label} benchmark evidence is incomplete")
        runtime_properties = evidence.get("runtime_properties")
        if (
            not is_managed_spark_22(evidence.get("runtime_version"))
            or not isinstance(runtime_properties, Mapping)
            or runtime_properties.get("dataproc.tier") != "standard"
        ):
            raise ValueError(f"{label} runtime contract is incomplete")
        missing = [name for name in REQUIRED_RUNTIME_METRICS if name not in evidence]
        if missing:
            raise ValueError(f"{label} benchmark metrics missing: {missing}")
    equivalence = validate_equivalence_evidence(checked, equivalence_evidence)
    identity = equivalence["equivalence_artifact_digest"]
    baseline_key = (
        float(baseline["application_runtime_seconds"]),
        float(baseline["dcu_hours"]),
    )
    optimized_key = (
        float(optimized["application_runtime_seconds"]),
        float(optimized["dcu_hours"]),
    )
    selected = "baseline" if baseline_key <= optimized_key else "optimized"
    selected_identity = output_identity(checked, selected)
    artifact: dict[str, object] = {
        "schema_version": WINNER_SCHEMA,
        "pair_manifest_digest": checked["pair_manifest_digest"],
        "benchmark_id": checked["benchmark_id"],
        "dataset_version": checked["dataset_version"],
        "git_sha": checked["git_sha"],
        "stage": checked["stage"],
        "upstream_run_ids": checked["upstream_run_ids"],
        "selected_profile": selected,
        "selected_output_run_id": selected_identity["output_run_id"],
        "selected_output_root": selected_identity["output_root"],
        "compared_outputs": equivalence["compared_outputs"],
        "equivalence_artifact_digest": identity,
    }
    artifact["winner_artifact_digest"] = winner_artifact_digest(artifact)
    return artifact
