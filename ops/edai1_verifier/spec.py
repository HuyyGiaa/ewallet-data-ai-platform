"""Workload specification: which campaign to verify and what it must contain."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

PROFILES = ("baseline", "optimized")
STAGES = ("bronze", "silver", "gold", "features")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA64 = re.compile(r"^[0-9a-f]{64}$")
_BENCHMARK = re.compile(r"^ab-[0-9]{14}$")


class SpecError(ValueError):
    """The workload specification is malformed."""


@dataclass(frozen=True)
class WorkloadSpec:
    """Expected identity and inventory of one completed campaign.

    Attributes:
        benchmark_id: Campaign benchmark, for example ``ab-20261110225456``.
        dataset_version: Approved dataset version of the campaign.
        source_git_sha: Git SHA that ran every Spark stage.
        source_manifest_sha256: SHA-256 of the immutable source manifest.
        rules_per_profile: Executed validation rules expected per profile.
        compared_tables: Delta tables expected in the semantic comparison.
        dag_runs: Successful Airflow DagRuns expected.
        task_instances: Successful Airflow task instances expected.
        tables_per_stage: Expected output tables per comparison stage.
    """

    benchmark_id: str
    dataset_version: str
    source_git_sha: str
    source_manifest_sha256: str
    rules_per_profile: int
    compared_tables: int
    dag_runs: int
    task_instances: int
    tables_per_stage: Mapping[str, int]

    def __post_init__(self) -> None:
        if not _BENCHMARK.fullmatch(self.benchmark_id):
            raise SpecError(f"invalid benchmark_id {self.benchmark_id!r}")
        if not _SHA40.fullmatch(self.source_git_sha):
            raise SpecError("source_git_sha must be a 40-character hex SHA")
        if not _SHA64.fullmatch(self.source_manifest_sha256):
            raise SpecError("source_manifest_sha256 must be a 64-character hex SHA-256")
        for name in ("rules_per_profile", "compared_tables", "dag_runs", "task_instances"):
            if int(getattr(self, name)) <= 0:
                raise SpecError(f"{name} must be positive")
        if sum(self.tables_per_stage.values()) != self.compared_tables:
            raise SpecError("tables_per_stage does not add up to compared_tables")

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready copy, used as part of the report."""

        value = asdict(self)
        value["tables_per_stage"] = dict(sorted(self.tables_per_stage.items()))
        return value


def load_spec(path: Path) -> WorkloadSpec:
    """Read a workload specification JSON file.

    Raises:
        SpecError: A field is missing or invalid.
    """

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecError(f"cannot read workload spec {path}: {exc}") from exc
    try:
        return WorkloadSpec(
            benchmark_id=str(raw["benchmark_id"]),
            dataset_version=str(raw["dataset_version"]),
            source_git_sha=str(raw["source_git_sha"]),
            source_manifest_sha256=str(raw["source_manifest_sha256"]),
            rules_per_profile=int(raw["rules_per_profile"]),
            compared_tables=int(raw["compared_tables"]),
            dag_runs=int(raw["dag_runs"]),
            task_instances=int(raw["task_instances"]),
            tables_per_stage={str(k): int(v) for k, v in raw["tables_per_stage"].items()},
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecError(f"invalid workload spec {path}: {exc}") from exc
