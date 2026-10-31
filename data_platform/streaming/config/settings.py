"""Controlled baseline and optimized profiles with identical semantics.

Profiles are loaded at runtime from ``configs/streaming/flink-<name>.yaml``;
event-time, dedup and history semantics come from the shared
``configs/streaming/block-a.yaml`` so both profiles cannot drift apart.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import yaml


CONFIG_DIR = Path(__file__).resolve().parents[3] / "configs" / "streaming"
STATE_BACKENDS = ("hashmap", "rocksdb")
CHECKPOINT_MODES = ("EXACTLY_ONCE", "AT_LEAST_ONCE")
_PROFILE_KEYS = {
    "profile",
    "parallelism",
    "checkpoint_interval_seconds",
    "state_backend",
    "incremental_checkpoints",
    "semantic_settings_ref",
    "checkpoint_mode",
    "kafka_delivery_contract",
    "restart_attempts",
    "restart_delay_seconds",
}
# Keys that earlier drafts declared but no connector implements.
_UNSUPPORTED_KEYS = {
    "sink_batch_size": (
        "sink_batch_size is not implemented: the append-only 'kafka' SQL sink has "
        "no row-count buffer (only upsert-kafka has sink.buffer-flush.max-rows), "
        "and producer batch.size is byte-based"
    ),
}
_NAMESPACE = re.compile(r"^bench-[a-z0-9][a-z0-9-]{0,47}$")


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
    incremental_checkpoints: bool
    state_backend: str = "rocksdb"
    checkpoint_mode: str = "EXACTLY_ONCE"
    restart_attempts: int = 3
    restart_delay: timedelta = timedelta(seconds=10)
    config_sha256: str = ""

    def __post_init__(self) -> None:
        if self.parallelism < 1:
            raise ValueError("parallelism must be >= 1")
        if self.checkpoint_interval <= timedelta(0):
            raise ValueError("checkpoint_interval must be positive")
        if self.state_backend not in STATE_BACKENDS:
            raise ValueError(f"unsupported state_backend: {self.state_backend}")
        if self.incremental_checkpoints and self.state_backend != "rocksdb":
            raise ValueError(
                "incremental_checkpoints requires the rocksdb state backend; "
                f"{self.state_backend} would silently ignore it"
            )
        if self.checkpoint_mode not in CHECKPOINT_MODES:
            raise ValueError(f"unsupported checkpoint_mode: {self.checkpoint_mode}")


class OperatingMode(str, Enum):
    FIRST_DEPLOY_BOOTSTRAP = "FIRST_DEPLOY_BOOTSTRAP"
    NORMAL_STREAMING = "NORMAL_STREAMING"
    RESTART_RECOVERY = "RESTART_RECOVERY"
    # Fresh, isolated, bounded replay used only by the local benchmark harness.
    BENCHMARK_REPLAY = "BENCHMARK_REPLAY"


@dataclass(frozen=True)
class RuntimeSettings:
    """Fail-closed deployment inputs shared by the operating modes."""

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
    namespace: str | None = None

    def __post_init__(self) -> None:
        if not self.bootstrap_servers:
            raise ValueError("bootstrap_servers is required")
        if not self.checkpoint_storage:
            raise ValueError("checkpoint_storage is required")
        if self.mode is OperatingMode.BENCHMARK_REPLAY:
            if not self.namespace or not _NAMESPACE.match(self.namespace):
                raise ValueError(
                    "BENCHMARK_REPLAY requires an isolated namespace matching "
                    f"{_NAMESPACE.pattern}"
                )
            if self.savepoint_path or self.silver_root or self.cutover_iso:
                raise ValueError("BENCHMARK_REPLAY starts fresh; savepoint/bootstrap inputs are not allowed")
            return
        if self.namespace is not None:
            raise ValueError("namespace is only valid for BENCHMARK_REPLAY")
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
        if self.mode in {
            OperatingMode.NORMAL_STREAMING,
            OperatingMode.RESTART_RECOVERY,
        } and not self.savepoint_path:
            raise ValueError(f"{self.mode.value} requires savepoint_path")


def _semantics(contract: dict) -> dict:
    event_time = contract["event_time"]
    return {
        "event_time": EventTimeSettings(
            max_out_of_orderness=timedelta(seconds=int(event_time["max_out_of_orderness_seconds"])),
            allowed_lateness=timedelta(seconds=int(event_time["allowed_lateness_seconds"])),
            idle_timeout=timedelta(seconds=int(event_time["idle_timeout_seconds"])),
        ),
        "dedup_ttl": timedelta(seconds=int(contract["dedup"]["ttl_seconds"])),
        "history_ttl": timedelta(seconds=int(contract["feature_state"]["history_ttl_seconds"])),
    }


def load_profile(path: Path | str, *, repo_root: Path | None = None) -> FlinkProfile:
    """Load and validate one profile YAML plus its referenced semantic contract."""
    path = Path(path)
    raw_profile = path.read_bytes()
    values = yaml.safe_load(raw_profile)
    if not isinstance(values, dict):
        raise ValueError(f"{path} must contain a mapping")
    for key, reason in _UNSUPPORTED_KEYS.items():
        if key in values:
            raise ValueError(f"{path}: {reason}")
    unknown, missing = set(values) - _PROFILE_KEYS, _PROFILE_KEYS - set(values)
    if unknown or missing:
        raise ValueError(f"{path}: unknown={sorted(unknown)}, missing={sorted(missing)}")
    root = repo_root or CONFIG_DIR.parents[1]
    semantic_path = root / values["semantic_settings_ref"]
    raw_contract = semantic_path.read_bytes()
    digest = hashlib.sha256(raw_profile + b"\0" + raw_contract).hexdigest()
    for key in ("incremental_checkpoints",):
        if not isinstance(values[key], bool):
            raise ValueError(f"{path}: {key} must be a boolean")
    return FlinkProfile(
        name=str(values["profile"]),
        **_semantics(yaml.safe_load(raw_contract)),
        parallelism=int(values["parallelism"]),
        checkpoint_interval=timedelta(seconds=int(values["checkpoint_interval_seconds"])),
        incremental_checkpoints=values["incremental_checkpoints"],
        state_backend=str(values["state_backend"]),
        checkpoint_mode=str(values["checkpoint_mode"]),
        restart_attempts=int(values["restart_attempts"]),
        restart_delay=timedelta(seconds=int(values["restart_delay_seconds"])),
        config_sha256=digest,
    )


def profile_by_name(name: str, config_dir: Path = CONFIG_DIR) -> FlinkProfile:
    if name not in {"baseline", "optimized"}:
        raise ValueError(f"unknown Flink profile: {name}")
    profile = load_profile(config_dir / f"flink-{name}.yaml")
    if profile.name != name:
        raise ValueError(f"flink-{name}.yaml declares profile {profile.name!r}")
    return profile


BASELINE = profile_by_name("baseline")
OPTIMIZED = profile_by_name("optimized")
