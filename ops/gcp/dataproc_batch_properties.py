"""Shared Dataproc Serverless batch properties with explicit opt-in profiles."""

from __future__ import annotations

import argparse


DEFAULT_PERFORMANCE_PROFILE = "default"
CANONICAL_PERFORMANCE_PROFILE = "canonical"
PERFORMANCE_PROFILES = (
    DEFAULT_PERFORMANCE_PROFILE,
    CANONICAL_PERFORMANCE_PROFILE,
)

_CANONICAL_RESOURCE_PROPERTIES = (
    "spark.dynamicAllocation.profile=performance",
    "spark.dynamicAllocation.initialExecutors=10",
    "spark.dynamicAllocation.maxExecutors=20",
)


def add_performance_profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--performance-profile",
        choices=PERFORMANCE_PROFILES,
        default=DEFAULT_PERFORMANCE_PROFILE,
        help=(
            "Dataproc resource profile. 'default' preserves bounded/pilot "
            "behavior; 'canonical' opts into the approved canonical settings."
        ),
    )


def build_dataproc_batch_properties(
    *,
    performance_tier: str,
    data_bucket: str,
    profile: str = DEFAULT_PERFORMANCE_PROFILE,
) -> tuple[str, ...]:
    if profile not in PERFORMANCE_PROFILES:
        raise ValueError(
            f"performance profile must be one of {PERFORMANCE_PROFILES}, got {profile!r}"
        )
    properties = [
        f"dataproc.tier={performance_tier}",
        "spark.sql.extensions=io.delta.sql.DeltaSparkSessionExtension",
        "spark.sql.catalog.spark_catalog="
        "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        "spark.sql.session.timeZone=UTC",
        "spark.eventLog.enabled=true",
        f"spark.eventLog.dir=gs://{data_bucket}/spark-events",
    ]
    if profile == CANONICAL_PERFORMANCE_PROFILE:
        properties.extend(_CANONICAL_RESOURCE_PROPERTIES)
    return tuple(properties)
