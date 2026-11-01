"""Controlled baseline and optimized profiles with identical semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path


@dataclass(frozen=True)
class EventTimeSettings:
    max_out_of_orderness: timedelta = timedelta(minutes=2)
    allowed_lateness: timedelta = timedelta(minutes=10)
    idle_timeout: timedelta = timedelta(seconds=30)


@dataclass(frozen=True)
class FlinkProfile:
    name: str
    event_time: EventTimeSettings
    dedup_ttl: timedelta
    history_ttl: timedelta
    parallelism: int
    checkpoint_interval: timedelta
    sink_batch_size: int
    incremental_checkpoints: bool


class OperatingMode(str, Enum):
    FIRST_DEPLOY_BOOTSTRAP = "FIRST_DEPLOY_BOOTSTRAP"
    NORMAL_STREAMING = "NORMAL_STREAMING"
    RESTART_RECOVERY = "RESTART_RECOVERY"


@dataclass(frozen=True)
class RuntimeSettings:
    """Fail-closed deployment inputs shared by the three operating modes."""

    mode: OperatingMode
    profile: FlinkProfile
    bootstrap_servers: str
    kafka_connector_jar: Path
    checkpoint_storage: str
    savepoint_path: str | None = None
    silver_root: str | None = None
    silver_manifest_uri: str | None = None
    source_run_id: str | None = None
    dataset_version: str | None = None
    cutover_iso: str | None = None

    def __post_init__(self) -> None:
        if not self.bootstrap_servers:
            raise ValueError("bootstrap_servers is required")
        if not self.checkpoint_storage:
            raise ValueError("checkpoint_storage is required")
        if self.mode is OperatingMode.FIRST_DEPLOY_BOOTSTRAP:
            required = {
                "silver_root": self.silver_root,
                "silver_manifest_uri": self.silver_manifest_uri,
                "source_run_id": self.source_run_id,
                "dataset_version": self.dataset_version,
                "cutover_iso": self.cutover_iso,
            }
            missing = sorted(name for name, value in required.items() if not value)
            if missing:
                raise ValueError(f"bootstrap mode missing: {missing}")
            parsed_cutover = datetime.fromisoformat(str(self.cutover_iso).replace("Z", "+00:00"))
            if parsed_cutover.tzinfo is None:
                raise ValueError("bootstrap cutover must be timezone-aware")
            object.__setattr__(self, "cutover_iso", parsed_cutover.astimezone(timezone.utc).isoformat())
        if self.mode is OperatingMode.RESTART_RECOVERY and not self.savepoint_path:
            raise ValueError("restart recovery requires savepoint_path")


def profile_by_name(name: str) -> FlinkProfile:
    try:
        return {"baseline": BASELINE, "optimized": OPTIMIZED}[name]
    except KeyError as error:
        raise ValueError(f"unknown Flink profile: {name}") from error


_SEMANTICS = {
    "event_time": EventTimeSettings(),
    "dedup_ttl": timedelta(hours=48),
    "history_ttl": timedelta(days=30, minutes=10),
}

BASELINE = FlinkProfile(
    name="baseline",
    **_SEMANTICS,
    parallelism=1,
    checkpoint_interval=timedelta(minutes=2),
    sink_batch_size=1,
    incremental_checkpoints=False,
)

OPTIMIZED = FlinkProfile(
    name="optimized",
    **_SEMANTICS,
    parallelism=4,
    checkpoint_interval=timedelta(seconds=30),
    sink_batch_size=250,
    incremental_checkpoints=True,
)
