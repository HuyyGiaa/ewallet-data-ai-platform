"""Measure one deterministic source shard and derive a target-size plan."""

from __future__ import annotations

import argparse
import json
import math
import resource
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from data_platform.generation.src.offline.offline_generator import load_config
from data_platform.generation.src.sharded.generator import generate_shard
from data_platform.generation.src.sharded.models import ShardManifest
from data_platform.generation.src.sharded.planner import (
    build_shard_plan,
    canonical_json_sha256,
)
from ops.gcp.generate_source_dataset import resolve_generator_config_path


class CalibrationError(ValueError):
    """Raised when a scale profile or derived plan is unsafe."""


@dataclass(frozen=True)
class CalibrationProfile:
    name: str
    target_bytes: int
    tolerance_ratio: float
    global_seed: int
    users_per_shard: int
    merchants_per_shard: int
    transactions_per_shard: int
    minimum_free_disk_bytes: int
    staging_safety_multiplier: float

    def __post_init__(self) -> None:
        positive_integers = {
            "target_bytes": self.target_bytes,
            "users_per_shard": self.users_per_shard,
            "merchants_per_shard": self.merchants_per_shard,
            "transactions_per_shard": self.transactions_per_shard,
            "minimum_free_disk_bytes": self.minimum_free_disk_bytes,
        }
        if not self.name:
            raise CalibrationError("profile name must not be empty")
        for field, value in positive_integers.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise CalibrationError(f"{field} must be a positive integer")
        if not 0 < self.tolerance_ratio < 1:
            raise CalibrationError("tolerance_ratio must be between 0 and 1")
        if self.staging_safety_multiplier < 1:
            raise CalibrationError("staging_safety_multiplier must be at least 1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CalibrationProfile":
        expected = set(cls.__dataclass_fields__)
        actual = set(value)
        unknown = actual - expected
        missing = expected - actual
        if unknown:
            raise CalibrationError(
                "unsupported keys: " + ", ".join(sorted(unknown))
            )
        if missing:
            raise CalibrationError(
                "missing keys: " + ", ".join(sorted(missing))
            )
        return cls(**{name: value[name] for name in expected})


def load_profile(path: Path) -> CalibrationProfile:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise CalibrationError("calibration profile must be a YAML mapping")
    return CalibrationProfile.from_dict(value)


def build_calibration_report(
    *,
    profile: CalibrationProfile,
    manifest: ShardManifest,
    runtime_seconds: float,
    peak_rss_bytes: int,
    free_disk_bytes: int,
    config_sha256: str,
    source_git_revision: str,
) -> dict[str, Any]:
    measured_bytes = sum(item.bytes for item in manifest.files)
    if measured_bytes <= 0 or manifest.logical_transactions <= 0:
        raise CalibrationError("calibration shard measurements must be positive")

    required_staging_bytes = math.ceil(
        measured_bytes * profile.staging_safety_multiplier
    )
    free_after_staging = free_disk_bytes - required_staging_bytes
    if free_after_staging < profile.minimum_free_disk_bytes:
        raise CalibrationError(
            "free-disk safety check failed: "
            f"free={free_disk_bytes}, required_staging={required_staging_bytes}, "
            f"minimum_remaining={profile.minimum_free_disk_bytes}"
        )

    shard_count = math.ceil(profile.target_bytes / measured_bytes)
    estimated_bytes = shard_count * measured_bytes
    minimum_bytes = math.floor(
        profile.target_bytes * (1 - profile.tolerance_ratio)
    )
    maximum_bytes = math.ceil(
        profile.target_bytes * (1 + profile.tolerance_ratio)
    )
    validated_label = (
        profile.name
        if minimum_bytes <= estimated_bytes <= maximum_bytes
        else None
    )

    return {
        "schema_version": 1,
        "inputs": {
            "profile": profile.to_dict(),
            "base_config_sha256": config_sha256,
            "source_git_revision": source_git_revision,
        },
        "measurement": {
            "calibration_logical_transactions": manifest.logical_transactions,
            "calibration_physical_transactions": manifest.physical_transactions,
            "total_parquet_bytes": measured_bytes,
            "bytes_per_logical_transaction": round(
                measured_bytes / manifest.logical_transactions, 6
            ),
            "bytes_by_table": {
                item.table: item.bytes for item in manifest.files
            },
            "rows_by_table": {
                item.table: item.rows for item in manifest.files
            },
            "runtime_seconds": round(runtime_seconds, 3),
            "peak_rss_bytes": peak_rss_bytes,
        },
        "disk_safety": {
            "free_before_bytes": free_disk_bytes,
            "required_staging_bytes": required_staging_bytes,
            "minimum_free_after_bytes": profile.minimum_free_disk_bytes,
            "estimated_free_after_bytes": free_after_staging,
            "pass": True,
        },
        "target": {
            "requested_bytes": profile.target_bytes,
            "tolerance_ratio": profile.tolerance_ratio,
            "accepted_byte_range": [minimum_bytes, maximum_bytes],
            "validated_label": validated_label,
        },
        "derived_plan": {
            "shard_count": shard_count,
            "users_per_shard": profile.users_per_shard,
            "merchants_per_shard": profile.merchants_per_shard,
            "transactions_per_shard": profile.transactions_per_shard,
            "logical_transactions": (
                shard_count * profile.transactions_per_shard
            ),
            "estimated_bytes": estimated_bytes,
        },
    }


def _peak_rss_bytes() -> int:
    # Linux ru_maxrss is KiB. Calibration runs in a fresh CLI process.
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)


def _git_revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure source Parquet density and derive a safe shard plan."
    )
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--config", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--staging-dir", type=Path, default=Path("tmp/source-calibration")
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    profile = load_profile(args.profile)
    base_config = load_config(resolve_generator_config_path(args.config))
    config_sha256 = canonical_json_sha256(base_config)

    staging_parent = args.staging_dir.expanduser().resolve()
    staging_parent.mkdir(parents=True, exist_ok=True)
    free_disk_bytes = shutil.disk_usage(staging_parent).free
    with tempfile.TemporaryDirectory(
        prefix="source-scale-", dir=staging_parent
    ) as temp_directory:
        output_root = Path(temp_directory) / "shards"
        calibration_plan = build_shard_plan(
            base_config=base_config,
            dataset_version=f"calibration-{profile.name}",
            global_seed=profile.global_seed,
            shard_count=1,
            users_per_shard=profile.users_per_shard,
            merchants_per_shard=profile.merchants_per_shard,
            transactions_per_shard=profile.transactions_per_shard,
        )
        started = time.perf_counter()
        measured_manifest = generate_shard(
            base_config, calibration_plan, 0, output_root
        )
        runtime_seconds = time.perf_counter() - started
        report = build_calibration_report(
            profile=profile,
            manifest=measured_manifest,
            runtime_seconds=runtime_seconds,
            peak_rss_bytes=_peak_rss_bytes(),
            free_disk_bytes=free_disk_bytes,
            config_sha256=config_sha256,
            source_git_revision=_git_revision(),
        )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["target"]["validated_label"] is None:
        raise CalibrationError(
            "derived estimate falls outside profile tolerance; reduce shard size"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
