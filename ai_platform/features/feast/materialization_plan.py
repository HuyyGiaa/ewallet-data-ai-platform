"""Decide and verify one incremental Feast Online materialization window.

This module holds the decisions behind the Airflow DAG and has no Feast, Airflow
or cloud dependency, so every rule can be tested without a cluster. The work
itself stays in :mod:`ai_platform.features.feast.online_operator`.

Invariants:

* The offline snapshot is immutable and named by run identifiers, never
  ``latest``. Every registered source must resolve inside that snapshot.
* A window starts where the Feast registry says the view stopped. A view that
  was never materialized is an error, so a run cannot become a full history load.
* A window may not end after the snapshot's last event, or after the streaming
  cutover. Past the snapshot the registry would record data it never loaded.
* Retrying is safe because the registry only advances after a job succeeds and
  Feast's Redis store skips rows that are not newer than the stored timestamp.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping, Sequence

from ai_platform.features.feast.cloud_boundary import _validate_id
from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.features.feast.redis_isolation import owner_record, target_fingerprint
from data_platform.config.dataset_identity import (
    CANONICAL_100GB,
    REHEARSAL_5GB,
    resolve_storage_context,
)


PLAN_SCHEMA = "feast-incremental-plan-v1"
PROVENANCE_SCHEMA = "feast-incremental-provenance-v1"
DEFAULT_MAX_WINDOW = timedelta(hours=6)
_JOB_NAME_LIMIT = 50
_REDIS_MINIMUM_FREE_RATIO = 0.10
EXECUTION_POLICIES = frozenset({"production", "verification"})


# Verification stores share the Redis server with production DB 0 but never DB 0.
# A verification store accepts any database of its own range: the campaign claim
# still requires an empty database or this exact campaign's owner marker, so a
# rerun takes the next empty database instead of flushing a used one. Each store
# has its own Secret and a disjoint range: DB 1 stays with canonical-verify (and
# the RAG DB 1 canary), so a rehearsal never edits or claims that Secret or DB.
REHEARSAL_ONLINE_DBS = frozenset(range(2, 8))
REHEARSAL_STREAM_DBS = frozenset(range(8, 16))

ONLINE_STORE_BINDINGS = {
    "canonical-online": {
        "redis_secret": "feast-online-redis",
        "redis_host": "redis.feature-store.svc.cluster.local",
        "redis_port": 6379,
        "redis_dbs": frozenset({0}),
        "dataset_versions": frozenset({CANONICAL_100GB}),
        "requires_campaign_ownership": False,
    },
    "canonical-verify": {
        "redis_secret": "feast-online-redis-verify",
        "redis_host": "redis.feature-store.svc.cluster.local",
        "redis_port": 6379,
        "redis_dbs": frozenset({1}),
        "dataset_versions": frozenset({CANONICAL_100GB}),
        "requires_campaign_ownership": True,
    },
    # Row 11: Offline snapshot -> Redis materialization.
    "rehearsal-online": {
        "redis_secret": "feast-online-redis-rehearsal",
        "redis_host": "redis.feature-store.svc.cluster.local",
        "redis_port": 6379,
        "redis_dbs": REHEARSAL_ONLINE_DBS,
        "dataset_versions": frozenset({REHEARSAL_5GB}),
        "requires_campaign_ownership": True,
    },
    # Row 12: streaming Online writer, a separate campaign with its own registry.
    "rehearsal-stream": {
        "redis_secret": "feast-online-redis-stream",
        "redis_host": "redis.feature-store.svc.cluster.local",
        "redis_port": 6379,
        "redis_dbs": REHEARSAL_STREAM_DBS,
        "dataset_versions": frozenset({REHEARSAL_5GB}),
        "requires_campaign_ownership": True,
    },
}


def binding_allows_database(online_store_id: str, database: int) -> bool:
    """True when ``database`` belongs to the store's approved Redis range."""

    return database in ONLINE_STORE_BINDINGS[online_store_id]["redis_dbs"]


def main_binding(argv: Sequence[str] | None = None) -> int:
    """Print ``secret dbs`` for one store, for shell runners (single source of truth)."""

    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ONLINE_STORE_BINDINGS:
        print(f"usage: binding <{'|'.join(sorted(ONLINE_STORE_BINDINGS))}>", file=sys.stderr)
        return 2
    binding = ONLINE_STORE_BINDINGS[args[0]]
    print(binding["redis_secret"], ",".join(str(db) for db in sorted(binding["redis_dbs"])))
    return 0


class WindowState(str, Enum):
    """What a run must do for the requested window."""

    NEW_WINDOW = "NEW_WINDOW"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    NO_NEW_DATA = "NO_NEW_DATA"


class JobAction(str, Enum):
    """How a run relates to Kubernetes Jobs already created for its window."""

    CREATE = "CREATE"
    ATTACH = "ATTACH"
    REUSE = "REUSE"


class WindowError(ValueError):
    """The requested window breaks the materialization contract."""


class ReadbackError(ValueError):
    """Online readback does not prove the expected window."""


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise WindowError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def parse_utc(value: str, name: str = "timestamp") -> datetime:
    """Parse an ISO-8601 instant, rejecting values without a UTC offset.

    Args:
        value: Timestamp text; a trailing ``Z`` is accepted.
        name: Field name used in error messages.

    Returns:
        The instant as an aware UTC datetime.
    """

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise WindowError(f"{name} is not ISO-8601: {value!r}") from error
    return _utc(parsed, name)


@dataclass(frozen=True)
class MaterializationRequest:
    """Immutable identity of one requested window.

    Attributes:
        bucket: Bare GCS bucket name.
        dataset_version: Canonical dataset identity.
        feature_run_id: DP3 run that produced the feature tables.
        feast_snapshot_id: Offline snapshot exported from that run.
        online_registry_id: Online registry that tracks the window.
        online_store_id: Approved logical Redis target binding.
        execution_policy: Explicit ``production`` or ``verification`` intent.
        redis_secret: Kubernetes Secret that resolves the Redis connection.
        estimated_redis_bytes: Owner-reviewed upper bound for this load.
        target_end: Inclusive end of the requested window.
        streaming_cutover: First instant owned by Flink, when streaming is live.
        max_window: Largest window one run may load.
    """

    bucket: str
    dataset_version: str
    feature_run_id: str
    feast_snapshot_id: str
    online_registry_id: str
    online_store_id: str
    execution_policy: str
    redis_secret: str
    estimated_redis_bytes: int
    target_end: datetime
    streaming_cutover: datetime | None = None
    max_window: timedelta = DEFAULT_MAX_WINDOW

    def __post_init__(self) -> None:
        if not self.bucket or self.bucket.startswith("gs://") or "/" in self.bucket:
            raise WindowError("bucket must be a bare GCS bucket name")
        for field in (
            "dataset_version",
            "feature_run_id",
            "feast_snapshot_id",
            "online_registry_id",
            "online_store_id",
        ):
            try:
                _validate_id(getattr(self, field), field)
            except ValueError as error:
                raise WindowError(str(error)) from error
        try:
            resolve_storage_context(self.dataset_version, self.bucket)
        except ValueError as error:
            raise WindowError(str(error)) from error
        binding = ONLINE_STORE_BINDINGS.get(self.online_store_id)
        if binding is None:
            raise WindowError(
                f"online_store_id must be one of {sorted(ONLINE_STORE_BINDINGS)}"
            )
        if self.redis_secret != binding["redis_secret"]:
            raise WindowError(
                f"{self.online_store_id} must use Redis secret {binding['redis_secret']}"
            )
        validate_execution_policy(self.execution_policy, self.online_store_id)
        if self.dataset_version not in binding["dataset_versions"]:
            raise WindowError(
                f"{self.dataset_version} cannot use {self.online_store_id}; "
                "rehearsal and canonical Online Stores are separate bindings"
            )
        if self.estimated_redis_bytes <= 0:
            raise WindowError("estimated_redis_bytes must be positive")
        object.__setattr__(self, "target_end", _utc(self.target_end, "target_end"))
        if self.streaming_cutover is not None:
            cutover = _utc(self.streaming_cutover, "streaming_cutover")
            object.__setattr__(self, "streaming_cutover", cutover)
            if self.target_end > cutover:
                raise WindowError(
                    "target_end is after the streaming cutover; Flink owns that range"
                )
        if self.max_window <= timedelta(0):
            raise WindowError("max_window must be positive")

    @property
    def snapshot_root(self) -> str:
        """Immutable Feast Offline snapshot root."""

        return (
            f"gs://{self.bucket}/features/offline/snapshots/"
            f"{self.dataset_version}/{self.feature_run_id}/{self.feast_snapshot_id}"
        )

    @property
    def registry_uri(self) -> str:
        """Online registry object that records materialization intervals."""

        return (
            f"gs://{self.bucket}/features/registries/{self.dataset_version}/"
            f"online/{self.online_registry_id}/registry.db"
        )

    @property
    def window_id(self) -> str:
        """Stable identity shared by every attempt at this target."""

        material = "\0".join((
            self.registry_uri,
            self.snapshot_root,
            self.online_store_id,
            self.target_end.isoformat(),
        ))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]

    def as_dict(self) -> dict[str, object]:
        """Return the JSON form recorded in run evidence."""

        return {
            "schema_version": PLAN_SCHEMA,
            "window_id": self.window_id,
            "bucket": self.bucket,
            "dataset_version": self.dataset_version,
            "feature_run_id": self.feature_run_id,
            "feast_snapshot_id": self.feast_snapshot_id,
            "online_registry_id": self.online_registry_id,
            "online_store_id": self.online_store_id,
            "execution_policy": self.execution_policy,
            "redis_secret": self.redis_secret,
            "estimated_redis_bytes": self.estimated_redis_bytes,
            "snapshot_root": self.snapshot_root,
            "registry_uri": self.registry_uri,
            "target_end": self.target_end.isoformat(),
            "streaming_cutover": (
                self.streaming_cutover.isoformat() if self.streaming_cutover else None
            ),
            "max_window_seconds": int(self.max_window.total_seconds()),
        }


def validate_execution_policy(policy: str, online_store_id: str) -> None:
    """Bind explicit production or verification intent to one store class."""

    if policy not in EXECUTION_POLICIES:
        raise WindowError(
            f"execution policy must be one of {sorted(EXECUTION_POLICIES)}"
        )
    binding = ONLINE_STORE_BINDINGS.get(online_store_id)
    if binding is None:
        raise WindowError("execution policy cannot resolve the Online Store binding")
    isolated = bool(binding["requires_campaign_ownership"])
    if policy == "verification" and not isolated:
        raise WindowError("verification execution policy cannot use the production Online Store")
    if policy == "production" and isolated:
        raise WindowError("production execution policy cannot use a verification Online Store")


def verify_online_store(
    request: MaterializationRequest, evidence: Mapping[str, object]
) -> dict[str, object]:
    """Verify the resolved Redis target and capacity without exposing credentials."""

    binding = ONLINE_STORE_BINDINGS[request.online_store_id]
    if evidence.get("logical_id") != request.online_store_id:
        raise WindowError("resolved Online Store logical identity differs from the request")
    if evidence.get("secret_name") != request.redis_secret:
        raise WindowError("resolved Online Store secret differs from the approved binding")
    if int(evidence.get("db", -1)) not in binding["redis_dbs"]:
        raise WindowError("resolved Redis database differs from the approved binding")
    if (
        evidence.get("host") != binding["redis_host"]
        or int(evidence.get("port", 0)) != int(binding["redis_port"])
    ):
        raise WindowError("resolved Redis host or port differs from the approved binding")
    actual_fingerprint = target_fingerprint(
        str(evidence.get("host", "")),
        int(evidence.get("port", 0)),
        int(evidence.get("db", -1)),
        str(evidence.get("server_run_id", "")),
    )
    if evidence.get("target_fingerprint") != actual_fingerprint:
        raise WindowError("resolved Redis target fingerprint is incorrect")
    if binding["requires_campaign_ownership"]:
        expected_owner = owner_record(
            request.online_registry_id,
            request.dataset_version,
            request.registry_uri,
            request.online_store_id,
            actual_fingerprint,
            request.feature_run_id,
            request.feast_snapshot_id,
        )
        if evidence.get("isolation_status") not in {"CLAIMED", "OWNED"}:
            raise WindowError("verification Redis isolation is not established")
        if evidence.get("ownership") != expected_owner:
            raise WindowError("verification Redis ownership differs from the campaign")
    if evidence.get("maxmemory_policy") != "noeviction":
        raise WindowError("Redis maxmemory policy must be noeviction")
    used = int(evidence.get("used_memory_bytes", -1))
    maximum = int(evidence.get("maxmemory_bytes", 0))
    if used < 0 or maximum <= 0 or used > maximum:
        raise WindowError("Redis memory evidence is invalid")
    headroom = maximum - used - request.estimated_redis_bytes
    if headroom < int(maximum * _REDIS_MINIMUM_FREE_RATIO):
        raise WindowError(
            "Redis memory preflight failed: estimated load would leave less than 10% headroom; "
            "use a separately approved Redis instance"
        )
    return {
        **dict(evidence),
        "estimated_impact_bytes": request.estimated_redis_bytes,
        "headroom_after_estimate_bytes": headroom,
        "minimum_free_ratio": _REDIS_MINIMUM_FREE_RATIO,
        "status": "PASS",
    }


@dataclass(frozen=True)
class ViewState:
    """What the registry and the snapshot report for one FeatureView."""

    view: str
    materialized_end: datetime | None
    source_path: str
    source_max: datetime

    @classmethod
    def from_evidence(cls, value: Mapping[str, object]) -> "ViewState":
        """Build a state from one view of the operator's ``state`` evidence."""

        end = value.get("materialized_end")
        return cls(
            view=str(value["view"]),
            materialized_end=parse_utc(str(end), "materialized_end") if end else None,
            source_path=str(value["source_path"]),
            source_max=parse_utc(str(value["source_max_event_timestamp"]), "source_max"),
        )


@dataclass(frozen=True)
class WindowDecision:
    """The window a run will load, or why it loads nothing."""

    state: WindowState
    start: datetime | None
    end: datetime
    pending_views: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, object]:
        """Return the JSON form recorded in run evidence."""

        return {
            "state": self.state.value,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat(),
            "pending_views": list(self.pending_views),
            "reason": self.reason,
        }


def validate_views(request: MaterializationRequest, views: Sequence[ViewState]) -> None:
    """Reject registry state that does not match the frozen contract.

    Raises:
        WindowError: A view is missing, unknown, never materialized, or bound to
            a source outside the requested snapshot.
    """

    names = sorted(view.view for view in views)
    if names != sorted(FEATURE_SPECS):
        raise WindowError(f"FeatureView set drifted: {names}")
    for view in views:
        expected = f"{request.snapshot_root}/{FEATURE_SPECS[view.view]['table']}"
        if view.source_path.rstrip("/") != expected:
            raise WindowError(
                f"{view.view} reads {view.source_path}, not snapshot {expected}"
            )
        if view.materialized_end is None:
            raise WindowError(
                f"{view.view} was never materialized; load an explicit base window "
                "first, because an incremental run must not become a full history load"
            )


def decide_window(
    request: MaterializationRequest, views: Sequence[ViewState]
) -> WindowDecision:
    """Classify the requested window against registry and snapshot state.

    Args:
        request: Requested window identity.
        views: Current state of all four FeatureViews.

    Returns:
        ``COMPLETED`` when every view already reached the target, ``NO_NEW_DATA``
        when every view already passed the end of the snapshot, ``PARTIAL`` when
        only some views are behind, and ``NEW_WINDOW`` otherwise.

    Raises:
        WindowError: The contract is broken, the target runs past the snapshot
            while data remains, or the window is larger than ``max_window``.
    """

    validate_views(request, views)
    end = request.target_end
    pending = [view for view in views if view.materialized_end < end]
    if not pending:
        return WindowDecision(
            WindowState.COMPLETED, None, end, (), "every view already reached the target"
        )
    if all(view.materialized_end > view.source_max for view in views):
        return WindowDecision(
            WindowState.NO_NEW_DATA,
            None,
            end,
            (),
            "every view already passed the last event of this immutable snapshot",
        )
    horizon = max(view.source_max for view in views)
    if end > horizon:
        raise WindowError(
            f"target_end {end.isoformat()} is after the snapshot's last event "
            f"{horizon.isoformat()}; the registry would record data it never loaded"
        )
    start = min(view.materialized_end for view in pending)
    if end - start > request.max_window:
        raise WindowError(
            f"window {start.isoformat()} to {end.isoformat()} exceeds max_window"
        )
    names = tuple(sorted(view.view for view in pending))
    if len(pending) == len(views):
        return WindowDecision(WindowState.NEW_WINDOW, start, end, names, "new window")
    return WindowDecision(
        WindowState.PARTIAL, start, end, names, "recovering views left behind the target"
    )


def job_name(kind: str, window_id: str, attempt: int) -> str:
    """Build a Kubernetes Job name for one operator step.

    Args:
        kind: Short lowercase step name, for example ``inc`` or ``parity``.
        window_id: Value of :attr:`MaterializationRequest.window_id`.
        attempt: One-based attempt number.

    Returns:
        A DNS-safe name accepted by ``scripts/mlops/feast_online_job.sh``.
    """

    if attempt < 1:
        raise WindowError("attempt must be at least 1")
    name = f"feast-{kind}-{window_id}-a{attempt}"
    if len(name) > _JOB_NAME_LIMIT:
        raise WindowError(f"job name is too long: {name}")
    return name


@dataclass(frozen=True)
class ExistingJob:
    """A Kubernetes Job already labelled with this window."""

    name: str
    active: int
    succeeded: int
    failed: int


def resolve_materialize_job(
    window_id: str, jobs: Sequence[ExistingJob]
) -> tuple[JobAction, str]:
    """Choose which Job a materialization attempt should use.

    A worker can die while its Job keeps running. Attaching to that Job, instead
    of starting another, keeps two writers off the same window.

    Returns:
        ``ATTACH`` for a running Job, ``REUSE`` for a succeeded Job, or ``CREATE``
        with the next attempt number after failed Jobs.
    """

    running = sorted(job.name for job in jobs if job.active > 0)
    if len(running) > 1:
        raise WindowError(f"more than one running job for window: {running}")
    if running:
        return JobAction.ATTACH, running[0]
    succeeded = sorted(job.name for job in jobs if job.succeeded > 0)
    if succeeded:
        return JobAction.REUSE, succeeded[-1]
    return JobAction.CREATE, job_name("inc", window_id, len(jobs) + 1)


def verify_completion(
    request: MaterializationRequest, views: Sequence[ViewState]
) -> None:
    """Confirm that the registry reached the target for every view.

    Raises:
        WindowError: A view stayed behind after its job reported success.
    """

    validate_views(request, views)
    behind = sorted(
        view.view for view in views if view.materialized_end < request.target_end
    )
    if behind:
        raise WindowError(f"registry did not reach the target for: {behind}")


def verify_readback(
    evidence: Mapping[str, object],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    """Check that online parity evidence proves the loaded window.

    Args:
        evidence: The JSON document printed by the operator's ``parity`` command.
        start: Lower bound of the entity sample.
        end: Parity cutover, which must equal the window end.

    Returns:
        A summary with comparison and entity counts.

    Raises:
        ReadbackError: Parity failed, was vacuous, used another cutover, or a
            value carries a timestamp outside the expected window.
    """

    if evidence.get("status") != "PASS":
        raise ReadbackError(f"online parity status is {evidence.get('status')!r}")
    if parse_utc(str(evidence.get("cutover")), "cutover") != end:
        raise ReadbackError("online parity ran at a different cutover")
    views = evidence.get("views")
    if not isinstance(views, list) or sorted(
        str(item.get("view")) for item in views
    ) != sorted(FEATURE_SPECS):
        raise ReadbackError("online parity does not cover all four FeatureViews")
    comparisons = int(evidence.get("comparisons", 0))
    if comparisons <= 0 or int(evidence.get("mismatch_count", 1)) != 0:
        raise ReadbackError("online parity compared nothing or found mismatches")
    checked = superseded = 0
    for item in views:
        if item.get("missing_entity_all_null") is not True:
            raise ReadbackError(f"{item['view']}: missing-entity probe returned values")
        superseded_entities = set(item.get("superseded_by_streaming", ()))
        superseded += len(superseded_entities)
        for entity, value in zip(item["entities"], item["online_event_timestamps"]):
            if entity in superseded_entities:
                continue
            if value is None:
                raise ReadbackError(f"{item['view']}: entity {entity} has no online row")
            stamp = parse_utc(str(value), "online_event_timestamp")
            if not start <= stamp <= end:
                raise ReadbackError(
                    f"{item['view']}: entity {entity} is at {value}, outside the window"
                )
            checked += 1
    if checked == 0:
        raise ReadbackError("no sampled entity belongs to this window")
    return {
        "comparisons": comparisons,
        "entities_checked": checked,
        "entities_superseded_by_streaming": superseded,
    }


def provenance_digest(value: Mapping[str, object]) -> str:
    """Return the SHA-256 of a provenance record without its own digest."""

    payload = {key: item for key, item in value.items() if key != "provenance_digest"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def existing_job_from_kubernetes(item: Mapping[str, object]) -> ExistingJob:
    """Convert one ``kubectl get jobs -o json`` item to an :class:`ExistingJob`."""

    status = item.get("status") or {}
    metadata = item.get("metadata") or {}
    return ExistingJob(
        name=str(metadata["name"]),
        active=int(status.get("active") or 0),
        succeeded=int(status.get("succeeded") or 0),
        failed=int(status.get("failed") or 0),
    )


__all__ = [
    "DEFAULT_MAX_WINDOW",
    "ExistingJob",
    "JobAction",
    "MaterializationRequest",
    "PROVENANCE_SCHEMA",
    "ReadbackError",
    "ViewState",
    "WindowDecision",
    "WindowError",
    "WindowState",
    "decide_window",
    "existing_job_from_kubernetes",
    "job_name",
    "parse_utc",
    "provenance_digest",
    "resolve_materialize_job",
    "validate_views",
    "verify_completion",
    "verify_readback",
]


if __name__ == "__main__":
    raise SystemExit(main_binding())
