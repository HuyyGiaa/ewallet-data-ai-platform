"""Shared Dataproc Serverless properties for explicit Spark profiles."""

from __future__ import annotations

import argparse

from data_platform.config.spark_profiles import (
    BASELINE_SPARK_PROFILE,
    OPTIMIZED_SPARK_PROFILE,
    SPARK_PROFILES,
    resolve_spark_profile,
)


DEFAULT_PERFORMANCE_PROFILE = "default"
CANONICAL_PERFORMANCE_PROFILE = "canonical"
PERFORMANCE_PROFILES = (DEFAULT_PERFORMANCE_PROFILE, CANONICAL_PERFORMANCE_PROFILE)


def add_performance_profile_argument(parser: argparse.ArgumentParser) -> None:
    """Retain the old CLI for pilot/default and canonical compatibility."""
    parser.add_argument(
        "--performance-profile",
        choices=PERFORMANCE_PROFILES,
        default=None,
        help=(
            "Dataproc resource profile. 'default' preserves bounded/pilot "
            "behavior; 'canonical' opts into the approved canonical settings."
        ),
    )


def add_spark_profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--spark-profile",
        choices=SPARK_PROFILES,
        default=None,
        help="Explicit full-scale Spark A/B execution profile.",
    )


def build_dataproc_batch_properties(
    *,
    performance_tier: str,
    data_bucket: str,
    profile: str = DEFAULT_PERFORMANCE_PROFILE,
) -> tuple[str, ...]:
    if profile == DEFAULT_PERFORMANCE_PROFILE:
        resolved = None
    else:
        resolved = resolve_spark_profile(profile)
    properties = [
        f"dataproc.tier={performance_tier}",
        "spark.sql.extensions=io.delta.sql.DeltaSparkSessionExtension",
        "spark.sql.catalog.spark_catalog="
        "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        "spark.sql.session.timeZone=UTC",
        "spark.eventLog.enabled=true",
        f"spark.eventLog.dir=gs://{data_bucket}/spark-events",
    ]
    if resolved is not None:
        properties.extend(resolved.spark_properties)
    return tuple(properties)


def resolve_submission_profile(
    *,
    spark_profile: str | None,
    performance_profile: str | None,
) -> str:
    """Resolve the new profile contract while preserving old CLI callers."""

    if spark_profile is None and performance_profile is None:
        return DEFAULT_PERFORMANCE_PROFILE
    if spark_profile is None:
        return str(performance_profile)
    if performance_profile is None:
        return spark_profile
    legacy = (
        OPTIMIZED_SPARK_PROFILE
        if performance_profile == CANONICAL_PERFORMANCE_PROFILE
        else performance_profile
    )
    if legacy != spark_profile:
        raise ValueError("conflicting --spark-profile and --performance-profile")
    return spark_profile


def profile_summary(profile: str) -> tuple[str, ...]:
    resolved = resolve_spark_profile(profile)
    lines = [
        f"SPARK_PROFILE: {resolved.name.upper()}",
        f"AQE: {str(resolved.adaptive_enabled).lower()}",
        f"SKEW_JOIN: {str(resolved.skew_join_enabled).lower()}",
        f"SHUFFLE_PARTITIONS: {resolved.shuffle_partitions}",
        "DYNAMIC_ALLOCATION: true",
        f"INITIAL_EXECUTORS: {resolved.initial_executors}",
        f"MAX_EXECUTORS: {resolved.max_executors}",
    ]
    if resolved.dynamic_allocation_profile:
        lines.append(
            "DYNAMIC_ALLOCATION_PROFILE: "
            f"{resolved.dynamic_allocation_profile}"
        )
    else:
        lines.append("DYNAMIC_ALLOCATION_PROFILE: platform-neutral")
    return tuple(lines)
