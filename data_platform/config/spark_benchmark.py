"""Fail-closed Method 2 stage-common-input benchmark contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from data_platform.config.spark_profiles import (
    parse_benchmark_run_id,
    resolve_spark_profile,
    resolve_spark_stage,
)


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


def select_stage_winner(
    *,
    baseline_evidence: Mapping[str, object],
    candidate_evidence: Mapping[str, object],
    equivalence_evidence: Mapping[str, object],
) -> str:
    """Select only after complete evidence and high-confidence equivalence."""

    identity_fields = (
        "benchmark_id",
        "dataset_version",
        "git_sha",
        "stage",
        "upstream_run_ids",
    )
    for label, evidence in (
        ("baseline", baseline_evidence),
        ("candidate", candidate_evidence),
    ):
        if evidence.get("status") != "PASS":
            raise ValueError(f"{label} benchmark evidence is incomplete")
        missing = [name for name in REQUIRED_RUNTIME_METRICS if name not in evidence]
        if missing:
            raise ValueError(f"{label} benchmark metrics missing: {missing}")
    for field in identity_fields:
        if baseline_evidence.get(field) != candidate_evidence.get(field):
            raise ValueError(f"winner evidence has different {field}")
    if equivalence_evidence.get("high_confidence_equivalence") is not True:
        raise ValueError("unequal or missing semantic evidence cannot produce a winner")
    for field in ("benchmark_id", "dataset_version", "git_sha", "stage"):
        if equivalence_evidence.get(field) != baseline_evidence.get(field):
            raise ValueError(f"equivalence evidence has different {field}")
    if equivalence_evidence.get("source_run_ids") != baseline_evidence.get(
        "upstream_run_ids"
    ):
        raise ValueError("equivalence evidence has different source_run_ids")

    baseline_key = (
        float(baseline_evidence["application_runtime_seconds"]),
        float(baseline_evidence["dcu_hours"]),
    )
    candidate_key = (
        float(candidate_evidence["application_runtime_seconds"]),
        float(candidate_evidence["dcu_hours"]),
    )
    return "baseline" if baseline_key <= candidate_key else "optimized"


def benchmark_manifest(
    *,
    plans: Sequence[StageRunPlan],
    resolved_properties: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    if len(plans) != 2:
        raise ValueError("a Method 2 stage benchmark requires exactly two plans")
    require_common_stage_inputs(plans[0], plans[1])
    return {
        "method": "stage-common-input",
        "stage": plans[0].stage,
        "upstream_run_ids": list(plans[0].upstream_run_ids),
        "runs": [
            {
                "profile": plan.profile,
                "output_run_id": plan.output_run_id,
                "output_root": plan.output_root,
                "spark_properties": dict(resolved_properties[plan.profile]),
            }
            for plan in plans
        ],
        "required_runtime_metrics": list(REQUIRED_RUNTIME_METRICS),
    }
