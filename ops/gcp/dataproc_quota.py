"""Read and wait for the Compute quotas needed by a Managed Spark batch."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


CPU_METRIC = "CPUS_ALL_REGIONS"
DISK_METRIC = "DISKS_TOTAL_GB"
TERMINAL_BATCH_STATES = {"CANCELLED", "FAILED", "SUCCEEDED"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("batch stateTime must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class QuotaSnapshot:
    checked_at: datetime
    cpu_limit: float
    cpu_usage: float
    disk_limit_gb: float
    disk_usage_gb: float

    @property
    def cpu_available(self) -> float:
        return self.cpu_limit - self.cpu_usage

    @property
    def disk_available_gb(self) -> float:
        return self.disk_limit_gb - self.disk_usage_gb

    def as_dict(self) -> dict[str, float | str]:
        return {
            "checked_at": _timestamp(self.checked_at),
            "cpu_limit": self.cpu_limit,
            "cpu_usage": self.cpu_usage,
            "cpu_available": self.cpu_available,
            "disk_limit_gb": self.disk_limit_gb,
            "disk_usage_gb": self.disk_usage_gb,
            "disk_available_gb": self.disk_available_gb,
        }


@dataclass(frozen=True)
class PredecessorBatch:
    batch_id: str
    state: str
    terminal_time: datetime

    def as_dict(self) -> dict[str, str]:
        return {
            "batch_id": self.batch_id,
            "state": self.state,
            "terminal_time": _timestamp(self.terminal_time),
        }


@dataclass(frozen=True)
class PollingPolicy:
    initial_interval_seconds: float = 15
    max_interval_seconds: float = 30
    timeout_seconds: float = 900

    def __post_init__(self) -> None:
        if self.initial_interval_seconds <= 0:
            raise ValueError("initial poll interval must be positive")
        if self.max_interval_seconds < self.initial_interval_seconds:
            raise ValueError("maximum poll interval must be at least the initial interval")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout must be positive")


class QuotaTimeoutError(RuntimeError):
    def __init__(self, evidence: dict[str, Any]) -> None:
        self.evidence = evidence
        observed = evidence["observed"]
        required = evidence["required"]["initial"]
        super().__init__(
            "Dataproc quota wait timed out after "
            f"{evidence['wait_seconds']:.1f}s: available CPU="
            f"{observed['cpu_available']}, disk={observed['disk_available_gb']} GB; "
            f"required CPU={required['vcpus']}, disk={required['disk_gb']} GB"
        )


def _metric(payload: Mapping[str, Any], name: str) -> tuple[float, float]:
    quotas = payload.get("quotas")
    if not isinstance(quotas, list):
        raise ValueError("quota response must contain a quotas list")
    matches = [item for item in quotas if isinstance(item, Mapping) and item.get("metric") == name]
    if len(matches) != 1:
        raise ValueError(f"quota metric {name!r} must appear exactly once")
    item = matches[0]
    limit, usage = item.get("limit"), item.get("usage")
    if isinstance(limit, bool) or isinstance(usage, bool):
        raise ValueError(f"quota metric {name!r} contains a non-numeric value")
    try:
        limit_value, usage_value = float(limit), float(usage)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"quota metric {name!r} contains a non-numeric value") from exc
    if limit_value < 0 or usage_value < 0:
        raise ValueError(f"quota metric {name!r} contains a negative value")
    return limit_value, usage_value


def parse_quota_documents(
    project_payload: Mapping[str, Any],
    region_payload: Mapping[str, Any],
    *,
    checked_at: datetime | None = None,
) -> QuotaSnapshot:
    cpu_limit, cpu_usage = _metric(project_payload, CPU_METRIC)
    disk_limit, disk_usage = _metric(region_payload, DISK_METRIC)
    return QuotaSnapshot(
        checked_at=checked_at or _utc_now(),
        cpu_limit=cpu_limit,
        cpu_usage=cpu_usage,
        disk_limit_gb=disk_limit,
        disk_usage_gb=disk_usage,
    )


class GcloudQuotaReader:
    """Read structured quota and predecessor metadata without mutations."""

    def __init__(
        self,
        project: str,
        region: str,
        *,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.project = project
        self.region = region
        self.runner = runner

    def _json(self, command: Sequence[str]) -> Mapping[str, Any]:
        completed = self.runner(
            tuple(command), capture_output=True, text=True, check=True
        )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("gcloud returned malformed JSON") from exc
        if not isinstance(value, Mapping):
            raise ValueError("gcloud JSON response must be an object")
        return value

    def snapshot(self) -> QuotaSnapshot:
        project_payload = self._json(
            (
                "gcloud", "compute", "project-info", "describe",
                f"--project={self.project}", "--format=json(quotas)",
            )
        )
        region_payload = self._json(
            (
                "gcloud", "compute", "regions", "describe", self.region,
                f"--project={self.project}", "--format=json(quotas)",
            )
        )
        return parse_quota_documents(project_payload, region_payload)

    def predecessor(self, batch_id: str) -> PredecessorBatch:
        payload = self._json(
            (
                "gcloud", "dataproc", "batches", "describe", batch_id,
                f"--project={self.project}", f"--region={self.region}",
                "--format=json(name,state,stateTime)",
            )
        )
        state = str(payload.get("state", ""))
        state_time = payload.get("stateTime")
        if state not in TERMINAL_BATCH_STATES:
            raise ValueError(f"predecessor batch is not terminal: {state!r}")
        if not isinstance(state_time, str) or not state_time:
            raise ValueError("predecessor batch is missing stateTime")
        return PredecessorBatch(
            batch_id=batch_id,
            state=state,
            terminal_time=_parse_timestamp(state_time),
        )


def _ready(snapshot: QuotaSnapshot, requirement: Mapping[str, Any]) -> bool:
    initial = requirement["initial"]
    return (
        snapshot.cpu_available >= float(initial["vcpus"])
        and snapshot.disk_available_gb >= float(initial["disk_gb"])
    )


def wait_for_quota(
    *,
    project: str,
    region: str,
    requirement: dict[str, dict[str, int]],
    read_snapshot: Callable[[], QuotaSnapshot],
    policy: PollingPolicy = PollingPolicy(),
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _utc_now,
    predecessor: PredecessorBatch | None = None,
    on_observation: Callable[[QuotaSnapshot, int, float], None] | None = None,
) -> dict[str, Any]:
    """Poll until both initial CPU and disk requirements are available."""

    started_monotonic = monotonic()
    first_check_time = now()
    interval = policy.initial_interval_seconds
    observations: list[dict[str, float | str]] = []
    while True:
        current = read_snapshot()
        observations.append(current.as_dict())
        elapsed = max(0.0, monotonic() - started_monotonic)
        if on_observation is not None:
            on_observation(current, len(observations), elapsed)
        if _ready(current, requirement):
            ready_time = now()
            result: dict[str, Any] = {
                "status": "READY",
                "project": project,
                "region": region,
                "required": requirement,
                "observed": current.as_dict(),
                "poll_count": len(observations),
                "wait_seconds": elapsed,
                "first_check_time": _timestamp(first_check_time),
                "quota_ready_time": _timestamp(ready_time),
                "observations": observations,
            }
            if predecessor is not None:
                result["predecessor_batch"] = predecessor.as_dict()
                result["release_latency_seconds"] = max(
                    0.0, (ready_time - predecessor.terminal_time).total_seconds()
                )
            return result
        if elapsed >= policy.timeout_seconds:
            evidence = {
                "status": "TIMEOUT",
                "project": project,
                "region": region,
                "required": requirement,
                "observed": current.as_dict(),
                "poll_count": len(observations),
                "wait_seconds": elapsed,
                "first_check_time": _timestamp(first_check_time),
                "observations": observations,
            }
            if predecessor is not None:
                evidence["predecessor_batch"] = predecessor.as_dict()
            raise QuotaTimeoutError(evidence)
        remaining = policy.timeout_seconds - elapsed
        sleeper(min(interval, remaining))
        interval = min(policy.max_interval_seconds, interval * 1.5)
