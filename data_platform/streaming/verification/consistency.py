"""Compare Flink's online features with a recomputation from offline history.

The accepted-event history is the offline record. Replaying it through the
dependency-free reference state gives the value each feature must hold, so a match
shows that the offline and online paths agree. The check has no access to Flink
state, which is what makes it independent.

History delivery is at-least-once, so records are deduplicated by
``transaction_id``. ``TOO_LATE`` events stay in history but never enter feature
state, matching the job.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.streaming.contracts.events import TransactionEvent
from data_platform.streaming.contracts.features import Ownership
from data_platform.streaming.feast.parity import ParityStatus, compare_feature
from data_platform.streaming.flink.feature_state import FeatureSnapshot, HistoricalFeatureState


REPORT_SCHEMA = "block-a-consistency-report-v1"
STATE_DISPOSITIONS = frozenset({"ON_TIME", "LATE_ACCEPTED"})


@dataclass(frozen=True)
class HistoryRecord:
    """One deduplicated accepted event and how Flink classified it."""

    event: TransactionEvent
    disposition: str


def parse_history(lines: Iterable[bytes | str]) -> tuple[list[HistoryRecord], int]:
    """Decode history lines and drop at-least-once repeats.

    Returns:
        The unique records and how many repeats were dropped.

    Raises:
        ValueError: One transaction carries two different dispositions.
    """

    unique: dict[str, HistoryRecord] = {}
    repeats = 0
    for line in lines:
        if not line or not line.strip():
            continue
        value = json.loads(line)
        record = HistoryRecord(TransactionEvent.from_dict(value["payload"]), str(value["disposition"]))
        known = unique.get(record.event.transaction_id)
        if known is None:
            unique[record.event.transaction_id] = record
            continue
        repeats += 1
        if known.disposition != record.disposition:
            raise ValueError(f"conflicting dispositions for {record.event.transaction_id}")
    return list(unique.values()), repeats


def expected_online_state(
    records: Sequence[HistoryRecord], excluded_transactions: Iterable[str] = ()
) -> dict[tuple[str, str], FeatureSnapshot]:
    """Replay history and return the newest snapshot per view and entity.

    Args:
        records: Deduplicated history.
        excluded_transactions: Events whose windows never closed, such as the
            scenario's closing event; their timers have not fired.

    Returns:
        Snapshots keyed by ``(view_name, entity_value)``.
    """

    excluded = set(excluded_transactions)
    events = sorted(
        (
            record.event
            for record in records
            if record.disposition in STATE_DISPOSITIONS
            and record.event.transaction_id not in excluded
        ),
        key=lambda event: (event.event_time, event.transaction_id),
    )
    state = HistoricalFeatureState()
    latest: dict[tuple[str, str], FeatureSnapshot] = {}
    group: list[TransactionEvent] = []

    def close_group() -> None:
        snapshots = state.snapshot_group(list(group))
        for per_view in snapshots.values():
            for snapshot in per_view.values():
                latest[(snapshot.view_name, snapshot.entity_value)] = snapshot
        group.clear()

    for event in events:
        if group and group[0].event_time != event.event_time:
            close_group()
        group.append(event)
    if group:
        close_group()
    return latest


def _online_time(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def compare_online_state(
    expected: Mapping[tuple[str, str], FeatureSnapshot],
    online: Mapping[tuple[str, str], Mapping[str, object]],
) -> dict[str, object]:
    """Compare every expected feature value and event timestamp with Feast.

    Args:
        expected: Output of :func:`expected_online_state`.
        online: Rows keyed like ``expected``; each maps feature names to values and
            holds the Feast timestamp under ``"__ts"``, in whole seconds.

    Returns:
        Counts per parity status, timestamp mismatches and every failure.
    """

    counts = {status.value: 0 for status in ParityStatus}
    failures: list[dict[str, object]] = []
    timestamp_mismatches = 0
    for key, snapshot in sorted(expected.items()):
        row = online.get(key, {})
        online_time = _online_time(row.get("__ts"))
        expected_seconds = int(snapshot.feature_timestamp.timestamp())
        if online_time is not None and int(online_time.timestamp()) != expected_seconds:
            timestamp_mismatches += 1
            failures.append({"key": list(key), "feature": "__ts", "status": "TIMESTAMP_MISMATCH",
                             "offline": expected_seconds, "online": int(online_time.timestamp())})
        for feature, value in snapshot.values.items():
            result = compare_feature(
                value, row.get(feature), snapshot.feature_timestamp, online_time, Ownership.HYBRID
            )
            counts[result.status.value] += 1
            if result.status is not ParityStatus.MATCH:
                failures.append({"key": list(key), "feature": feature, "status": result.status.value,
                                 "offline": value, "online": row.get(feature)})
    return {"counts": counts, "timestamp_mismatches": timestamp_mismatches, "failures": failures}


def check_dispositions(
    records: Sequence[HistoryRecord], manifest: Mapping[str, object]
) -> list[str]:
    """Return every way the history disagrees with the scenario's expected outcome."""

    expected = manifest["expected"]
    observed = {record.event.transaction_id: record.disposition for record in records}
    problems = []
    for transaction in expected["on_time"]:
        if observed.get(transaction) != "ON_TIME":
            problems.append(f"{transaction}: expected ON_TIME, saw {observed.get(transaction)}")
    for name, disposition in (("late_accepted", "LATE_ACCEPTED"), ("too_late", "TOO_LATE")):
        for transaction in expected[name]:
            if observed.get(transaction) != disposition:
                problems.append(f"{transaction}: expected {disposition}, saw {observed.get(transaction)}")
    unique = len(set(expected["on_time"]) | set(expected["late_accepted"]) | set(expected["too_late"])
                 | set(expected["closing"]))
    if len(observed) != unique:
        problems.append(f"history holds {len(observed)} transactions, expected {unique}")
    return problems


def build_report(
    manifest: Mapping[str, object],
    records: Sequence[HistoryRecord],
    repeats: int,
    expected: Mapping[tuple[str, str], FeatureSnapshot],
    comparison: Mapping[str, object],
    disposition_problems: Sequence[str],
    identity: Mapping[str, object],
) -> dict[str, object]:
    """Assemble the machine-readable result and decide PASS or FAIL.

    PASS needs a non-empty comparison in which every feature matches, every
    timestamp agrees and every record has its expected disposition.
    """

    counts = comparison["counts"]
    compared = sum(counts.values())
    clean = (
        compared > 0
        and counts[ParityStatus.MATCH.value] == compared
        and comparison["timestamp_mismatches"] == 0
        and not disposition_problems
    )
    report: dict[str, object] = {
        "schema_version": REPORT_SCHEMA,
        "status": "PASS" if clean else "FAIL",
        "namespace": manifest["namespace"],
        "manifest_payload_sha256": manifest["payload_sha256"],
        "history_transactions": len(records),
        "history_repeats_dropped": repeats,
        "entities_compared": len(expected),
        "features_compared": compared,
        "feature_views": sorted({view for view, _ in expected}),
        "comparison": comparison,
        "disposition_problems": list(disposition_problems),
        "identity": dict(identity),
    }
    encoded = json.dumps(report, sort_keys=True, default=str).encode("utf-8")
    report["report_digest"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return report


def online_rows_from_feast(store, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], dict[str, object]]:
    """Read current online rows for ``(view, entity)`` keys through Feast."""

    rows: dict[tuple[str, str], dict[str, object]] = {}
    by_view: dict[str, list[str]] = {}
    for view, entity in keys:
        by_view.setdefault(view, []).append(entity)
    for view, entities in by_view.items():
        spec = FEATURE_SPECS[view]
        result = store.get_online_features(
            features=[f"{view}:{feature}" for feature in spec["features"]],
            entity_rows=[{spec["entity_key"]: entity} for entity in entities],
            full_feature_names=True,
        ).to_dict(include_event_timestamps=True)
        for index, entity in enumerate(entities):
            row: dict[str, object] = {
                feature: result[f"{view}__{feature}"][index] for feature in spec["features"]
            }
            row["__ts"] = result[f"{view}__{spec['features'][0]}__ts"][index] or None
            rows[(view, entity)] = row
    return rows
