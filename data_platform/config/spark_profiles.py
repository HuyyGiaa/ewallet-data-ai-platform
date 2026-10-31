"""Stage-aware Spark execution profiles and benchmark lineage rules.

``baseline`` and ``optimized`` remain benchmark identities so historical run
IDs stay readable. Runtime properties are resolved from both the stage and
identity; there is deliberately no global optimized tuning package.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


BASELINE_SPARK_PROFILE = "baseline"
OPTIMIZED_SPARK_PROFILE = "optimized"
SPARK_PROFILES = (BASELINE_SPARK_PROFILE, OPTIMIZED_SPARK_PROFILE)

BRONZE_STAGE = "dp1-bronze"
SILVER_STAGE = "dp2-silver"
GOLD_STAGE = "dp2-gold"
FEATURES_STAGE = "dp3-features"
SPARK_STAGES = (BRONZE_STAGE, SILVER_STAGE, GOLD_STAGE, FEATURES_STAGE)

_STAGE_ALIASES = {
    "bronze": BRONZE_STAGE,
    "silver": SILVER_STAGE,
    "gold": GOLD_STAGE,
    "feat": FEATURES_STAGE,
}
_BENCHMARK_ID = re.compile(r"^ab-[0-9]{14}$")
_ATTEMPT_ID = re.compile(r"^r[0-9]{12}$")
_RUN_STAGES = {"bronze", "silver", "gold", "feat", "dp1", "dp2", "dp3"}
_RUN_ID = re.compile(
    r"^(ab-[0-9]{14})-(baseline|optimized)-"
    r"(bronze|silver|gold|feat|dp1|dp2|dp3)-(r[0-9]{12})$"
)
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class SparkResourceContract:
    driver_cores: int = 4
    executor_cores: int = 4
    driver_memory: str = "9600m"
    executor_memory: str = "9600m"
    driver_memory_overhead: str = "3840m"
    executor_memory_overhead: str = "3840m"
    min_executors: int = 2
    initial_executors: int = 6
    max_executors: int = 7
    executor_allocation_ratio: float = 0.3
    broadcast_threshold: str = "192m"
    executor_idle_timeout: str = "300s"

    @property
    def spark_properties(self) -> tuple[str, ...]:
        return (
            f"spark.driver.cores={self.driver_cores}",
            f"spark.executor.cores={self.executor_cores}",
            f"spark.driver.memory={self.driver_memory}",
            f"spark.executor.memory={self.executor_memory}",
            f"spark.driver.memoryOverhead={self.driver_memory_overhead}",
            f"spark.executor.memoryOverhead={self.executor_memory_overhead}",
            "spark.dynamicAllocation.enabled=true",
            f"spark.dynamicAllocation.minExecutors={self.min_executors}",
            f"spark.dynamicAllocation.initialExecutors={self.initial_executors}",
            f"spark.dynamicAllocation.maxExecutors={self.max_executors}",
            "spark.dynamicAllocation.executorAllocationRatio="
            f"{self.executor_allocation_ratio}",
            f"spark.sql.autoBroadcastJoinThreshold={self.broadcast_threshold}",
            f"spark.dynamicAllocation.executorIdleTimeout={self.executor_idle_timeout}",
        )


COMMON_RESOURCE_CONTRACT = SparkResourceContract()


@dataclass(frozen=True)
class SparkProfileIdentity:
    name: str

    @property
    def initial_executors(self) -> int:
        return COMMON_RESOURCE_CONTRACT.initial_executors

    @property
    def max_executors(self) -> int:
        return COMMON_RESOURCE_CONTRACT.max_executors


BASELINE_PROFILE = SparkProfileIdentity(BASELINE_SPARK_PROFILE)
OPTIMIZED_PROFILE = SparkProfileIdentity(OPTIMIZED_SPARK_PROFILE)
_PROFILE_IDENTITIES = {
    BASELINE_PROFILE.name: BASELINE_PROFILE,
    OPTIMIZED_PROFILE.name: OPTIMIZED_PROFILE,
}


@dataclass(frozen=True)
class SparkExecutionProfile:
    stage: str
    name: str
    adaptive_enabled: bool
    skew_join_enabled: bool
    shuffle_partitions: int
    dynamic_allocation_profile: str | None = None
    resources: SparkResourceContract = COMMON_RESOURCE_CONTRACT

    @property
    def stage_profile(self) -> str:
        return f"{self.stage}:{self.name}"

    @property
    def initial_executors(self) -> int:
        return self.resources.initial_executors

    @property
    def max_executors(self) -> int:
        return self.resources.max_executors

    @property
    def spark_properties(self) -> tuple[str, ...]:
        properties = [
            *self.resources.spark_properties,
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

    def evidence(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "profile": self.name,
            "stage_profile": self.stage_profile,
            "adaptive_enabled": self.adaptive_enabled,
            "skew_join_enabled": self.skew_join_enabled,
            "shuffle_partitions": self.shuffle_partitions,
            "dynamic_allocation_profile": self.dynamic_allocation_profile,
            "resources": asdict(self.resources),
            "spark_properties": dict(
                item.split("=", 1) for item in self.spark_properties
            ),
        }


def _profile(
    stage: str,
    name: str,
    *,
    adaptive: bool,
    shuffle: int,
    allocation_profile: str | None = None,
) -> SparkExecutionProfile:
    return SparkExecutionProfile(
        stage=stage,
        name=name,
        adaptive_enabled=adaptive,
        skew_join_enabled=False,
        shuffle_partitions=shuffle,
        dynamic_allocation_profile=allocation_profile,
    )


_STAGE_PROFILES = {
    (stage, BASELINE_SPARK_PROFILE): _profile(
        stage, BASELINE_SPARK_PROFILE, adaptive=False, shuffle=200
    )
    for stage in SPARK_STAGES
}
_STAGE_PROFILES.update(
    {
        (BRONZE_STAGE, OPTIMIZED_SPARK_PROFILE): _profile(
            BRONZE_STAGE,
            OPTIMIZED_SPARK_PROFILE,
            adaptive=True,
            shuffle=200,
            allocation_profile="performance",
        ),
        (SILVER_STAGE, OPTIMIZED_SPARK_PROFILE): _profile(
            SILVER_STAGE, OPTIMIZED_SPARK_PROFILE, adaptive=False, shuffle=304
        ),
        (GOLD_STAGE, OPTIMIZED_SPARK_PROFILE): _profile(
            GOLD_STAGE, OPTIMIZED_SPARK_PROFILE, adaptive=True, shuffle=400
        ),
        (FEATURES_STAGE, OPTIMIZED_SPARK_PROFILE): _profile(
            FEATURES_STAGE, OPTIMIZED_SPARK_PROFILE, adaptive=False, shuffle=200
        ),
    }
)


def resolve_spark_profile(value: str) -> SparkProfileIdentity:
    """Resolve benchmark identity only; this function never chooses tuning."""

    normalized = value.strip().lower()
    try:
        return _PROFILE_IDENTITIES[normalized]
    except KeyError as exc:
        raise ValueError(
            f"spark profile must be one of {SPARK_PROFILES}, got {value!r}"
        ) from exc


def resolve_spark_stage(value: str) -> str:
    normalized = value.strip().lower()
    normalized = _STAGE_ALIASES.get(normalized, normalized)
    if normalized not in SPARK_STAGES:
        raise ValueError(f"Spark stage must be one of {SPARK_STAGES}, got {value!r}")
    return normalized


def resolve_stage_profile(stage: str, profile: str) -> SparkExecutionProfile:
    resolved_stage = resolve_spark_stage(stage)
    resolved_profile = resolve_spark_profile(profile).name
    try:
        return _STAGE_PROFILES[(resolved_stage, resolved_profile)]
    except KeyError as exc:
        raise ValueError(
            f"no Spark profile for stage={resolved_stage!r}, "
            f"profile={resolved_profile!r}"
        ) from exc


def quota_ceiling(
    profile: str,
    *,
    process_vcpus: int = 4,
    process_disk_gb: int = 400,
) -> dict[str, int]:
    return quota_requirements(
        profile,
        process_vcpus=process_vcpus,
        process_disk_gb=process_disk_gb,
    )["maximum"]


def quota_requirements(
    profile: str,
    *,
    process_vcpus: int = 4,
    process_disk_gb: int = 400,
) -> dict[str, dict[str, int]]:
    if process_vcpus <= 0 or process_disk_gb <= 0:
        raise ValueError("Dataproc process resources must be positive")
    resolve_spark_profile(profile)

    def resources(executors: int) -> dict[str, int]:
        process_count = 1 + executors
        return {
            "process_count": process_count,
            "vcpus": process_count * process_vcpus,
            "disk_gb": process_count * process_disk_gb,
        }

    return {
        "initial": resources(COMMON_RESOURCE_CONTRACT.initial_executors),
        "maximum": resources(COMMON_RESOURCE_CONTRACT.max_executors),
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
            "canonical Feast/ML inputs require selected optimized Spark lineage; "
            "baseline is benchmark-only"
        )
