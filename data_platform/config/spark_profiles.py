"""Shared Spark execution profiles and full-scale benchmark lineage rules."""

from __future__ import annotations

import re
from dataclasses import dataclass


BASELINE_SPARK_PROFILE = "baseline"
OPTIMIZED_SPARK_PROFILE = "optimized"
SPARK_PROFILES = (BASELINE_SPARK_PROFILE, OPTIMIZED_SPARK_PROFILE)

_PROFILE_ALIASES = {"canonical": OPTIMIZED_SPARK_PROFILE}
_BENCHMARK_ID = re.compile(r"^ab-[0-9]{14}$")
_ATTEMPT_ID = re.compile(r"^r[0-9]{12}$")
_RUN_STAGES = {"bronze", "silver", "gold", "feat", "dp1", "dp2", "dp3"}
_RUN_ID = re.compile(
    r"^(ab-[0-9]{14})-(baseline|optimized)-"
    r"(bronze|silver|gold|feat|dp1|dp2|dp3)-(r[0-9]{12})$"
)
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class SparkExecutionProfile:
    name: str
    adaptive_enabled: bool
    skew_join_enabled: bool
    shuffle_partitions: int
    initial_executors: int
    max_executors: int
    dynamic_allocation_profile: str | None

    @property
    def spark_properties(self) -> tuple[str, ...]:
        properties = [
            "spark.dynamicAllocation.enabled=true",
            f"spark.dynamicAllocation.initialExecutors={self.initial_executors}",
            f"spark.dynamicAllocation.maxExecutors={self.max_executors}",
            f"spark.sql.shuffle.partitions={self.shuffle_partitions}",
            f"spark.sql.adaptive.enabled={str(self.adaptive_enabled).lower()}",
            "spark.sql.adaptive.skewJoin.enabled="
            f"{str(self.skew_join_enabled).lower()}",
        ]
        if self.dynamic_allocation_profile is not None:
            properties.append(
                "spark.dynamicAllocation.profile="
                f"{self.dynamic_allocation_profile}"
            )
        return tuple(properties)


BASELINE_PROFILE = SparkExecutionProfile(
    name=BASELINE_SPARK_PROFILE,
    adaptive_enabled=False,
    skew_join_enabled=False,
    shuffle_partitions=200,
    initial_executors=6,
    max_executors=7,
    dynamic_allocation_profile=None,
)
OPTIMIZED_PROFILE = SparkExecutionProfile(
    name=OPTIMIZED_SPARK_PROFILE,
    adaptive_enabled=True,
    skew_join_enabled=True,
    shuffle_partitions=400,
    initial_executors=6,
    max_executors=7,
    dynamic_allocation_profile="performance",
)
_PROFILES = {
    BASELINE_PROFILE.name: BASELINE_PROFILE,
    OPTIMIZED_PROFILE.name: OPTIMIZED_PROFILE,
}


def resolve_spark_profile(value: str) -> SparkExecutionProfile:
    normalized = _PROFILE_ALIASES.get(value.strip().lower(), value.strip().lower())
    try:
        return _PROFILES[normalized]
    except KeyError as exc:
        raise ValueError(
            f"spark profile must be one of {SPARK_PROFILES}, got {value!r}"
        ) from exc


def quota_ceiling(
    profile: str,
    *,
    process_vcpus: int = 4,
    process_disk_gb: int = 400,
) -> dict[str, int]:
    resolved = resolve_spark_profile(profile)
    process_count = 1 + resolved.max_executors
    return {
        "process_count": process_count,
        "vcpus": process_count * process_vcpus,
        "disk_gb": process_count * process_disk_gb,
    }


def validate_benchmark_id(value: str) -> str:
    if not _BENCHMARK_ID.fullmatch(value):
        raise ValueError("benchmark_id must match ab-YYYYMMDDhhmmss")
    return value


def benchmark_run_id(
    benchmark_id: str,
    profile: str,
    stage: str,
    attempt_id: str,
) -> str:
    benchmark_id = validate_benchmark_id(benchmark_id)
    profile = resolve_spark_profile(profile).name
    if stage not in _RUN_STAGES:
        raise ValueError(f"unsupported benchmark stage: {stage}")
    if not _ATTEMPT_ID.fullmatch(attempt_id):
        raise ValueError("attempt_id must match rYYMMDDhhmmss")
    value = f"{benchmark_id}-{profile}-{stage}-{attempt_id}"
    if len(value) > 50:
        raise ValueError("benchmark run ID exceeds the GCP 50-character limit")
    return value


def require_profile_binding(
    run_id: str,
    *,
    benchmark_id: str,
    profile: str,
    stage: str,
) -> str:
    benchmark_id = validate_benchmark_id(benchmark_id)
    profile = resolve_spark_profile(profile).name
    if stage not in _RUN_STAGES:
        raise ValueError(f"unsupported benchmark stage: {stage}")
    prefix = f"{benchmark_id}-{profile}-{stage}-"
    if not run_id.startswith(prefix) or not _ATTEMPT_ID.fullmatch(
        run_id.removeprefix(prefix)
    ):
        raise ValueError(
            f"{stage} run {run_id!r} is not bound to benchmark "
            f"{benchmark_id!r} and profile {profile!r}"
        )
    return run_id


def parse_benchmark_run_id(run_id: str) -> dict[str, str]:
    """Parse a profile-bound immutable run ID into trusted lineage fields."""

    match = _RUN_ID.fullmatch(run_id)
    if match is None:
        raise ValueError(f"run ID is not a benchmark lineage ID: {run_id!r}")
    benchmark_id, profile, stage, attempt_id = match.groups()
    return {
        "benchmark_id": benchmark_id,
        "spark_profile": profile,
        "stage": stage,
        "attempt_id": attempt_id,
    }


def validate_git_sha(value: str) -> str:
    normalized = value.strip().lower()
    if not _GIT_SHA.fullmatch(normalized):
        raise ValueError("git_sha must be a full 40-character lowercase hex SHA")
    return normalized


def require_benchmark_pair(
    *, spark_profile: str | None, benchmark_id: str | None
) -> None:
    """Prevent partially-bound A/B submissions from bypassing lineage checks."""

    if bool(spark_profile) != bool(benchmark_id):
        raise ValueError("--spark-profile and --benchmark-id must be supplied together")


def require_optimized_lineage(
    profile: str,
    *,
    allow_baseline_for_test: bool = False,
) -> None:
    resolved = resolve_spark_profile(profile)
    if resolved.name != OPTIMIZED_SPARK_PROFILE and not allow_baseline_for_test:
        raise ValueError(
            "canonical Feast/ML inputs require optimized Spark lineage; "
            "baseline is benchmark-only"
        )
