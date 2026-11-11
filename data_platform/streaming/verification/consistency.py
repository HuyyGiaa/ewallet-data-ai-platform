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
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.streaming.contracts.events import TransactionEvent
from data_platform.streaming.contracts.features import Ownership
from data_platform.streaming.feast.parity import ParityStatus, compare_feature
from data_platform.streaming.flink.feature_state import FeatureSnapshot, HistoricalFeatureState


REPORT_SCHEMA = "block-a-consistency-report-v2"
STATE_DISPOSITIONS = frozenset({"ON_TIME", "LATE_ACCEPTED"})
SHA256_REPORT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class HistoryRecord:
    """One deduplicated accepted event and how Flink classified it."""

    event: TransactionEvent
    disposition: str


def report_digest(report: Mapping[str, object]) -> str:
    """Return the canonical digest, excluding the digest field itself."""

    payload = {key: value for key, value in report.items() if key != "report_digest"}
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_report_contract(report: Mapping[str, object]) -> None:
    """Reject an incomplete, failed or vacuous consistency report."""

    try:
        comparison = report["comparison"]
        source = report["source_event_verification"]
        offline = report["offline_verification"]
        direct = report["direct_offline_online_comparison"]
        counts = comparison["counts"]
        features = int(report["features_compared"])
        entities = int(report["entities_compared"])
        direct_comparisons = int(direct["comparisons"])
        direct_mismatches = int(direct["mismatch_count"])
        source_comparisons = int(source["comparisons"])
        rows_retrieved = int(offline["rows_retrieved"])
        timestamp_mismatches = int(comparison["timestamp_mismatches"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("consistency report is missing required fields") from error
    mismatch_counts = sum(
        int(value) for name, value in counts.items() if name != ParityStatus.MATCH.value
    )
    declared_digest = report.get("report_digest")
    digest_valid = (
        isinstance(declared_digest, str)
        and SHA256_REPORT_DIGEST.fullmatch(declared_digest) is not None
        and hmac.compare_digest(declared_digest, report_digest(report))
    )
    valid = (
        report.get("schema_version") == REPORT_SCHEMA
        and report.get("status") == "PASS"
        and features > 0
        and entities > 0
        and source.get("status") == "PASS"
        and source_comparisons == features
        and offline.get("status") == "PASS"
        and rows_retrieved > 0
        and direct.get("status") == "PASS"
        and direct_comparisons > 0
        and direct_mismatches == 0
        and int(counts.get(ParityStatus.MATCH.value, -1)) == features
        and mismatch_counts == 0
        and timestamp_mismatches == 0
        and digest_valid
    )
    if not valid:
        raise ValueError("consistency report does not satisfy the PASS contract")


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
    offline_verification: Mapping[str, object] | None = None,
    direct_comparison: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Assemble the machine-readable result and decide PASS or FAIL.

    PASS needs a non-empty comparison in which every feature matches, every
    timestamp agrees and every record has its expected disposition.
    """

    counts = comparison["counts"]
    compared = sum(counts.values())
    source_clean = (
        compared > 0
        and counts[ParityStatus.MATCH.value] == compared
        and comparison["timestamp_mismatches"] == 0
        and not disposition_problems
    )
    clean = source_clean
    if offline_verification is not None:
        clean = clean and offline_verification.get("status") == "PASS"
    if direct_comparison is not None:
        clean = (
            clean
            and direct_comparison.get("status") == "PASS"
            and int(direct_comparison.get("comparisons", 0)) > 0
            and int(direct_comparison.get("mismatch_count", -1)) == 0
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
        "source_event_verification": {
            "status": "PASS" if source_clean else "FAIL",
            "transactions": len(records),
            "comparisons": compared,
        },
    }
    if offline_verification is not None:
        report["offline_verification"] = dict(offline_verification)
    if direct_comparison is not None:
        report["direct_offline_online_comparison"] = dict(direct_comparison)
    report["report_digest"] = report_digest(report)
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


def offline_rows_from_feast(
    store,
    keys: Mapping[tuple[str, str], datetime],
) -> dict[tuple[str, str], dict[str, object]]:
    """Verify Feast PIT values against the authoritative immutable row.

    Feast does not expose the selected source row timestamp. The verifier therefore
    derives the latest eligible row independently from immutable Parquet, preserves
    its real timestamp, and rejects the stronger identity claim when multiple
    eligible rows have the same returned values.
    """

    import pandas as pd
    import pyarrow.parquet as pq
    from fsspec.core import url_to_fs

    rows: dict[tuple[str, str], dict[str, object]] = {}
    by_view: dict[str, list[tuple[str, datetime]]] = {}
    for (view, entity), timestamp in keys.items():
        if view not in FEATURE_SPECS:
            raise ValueError(f"unknown FeatureView: {view}")
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("offline query timestamps must be UTC-aware")
        by_view.setdefault(view, []).append((entity, timestamp.astimezone(timezone.utc)))
    for view, values in by_view.items():
        spec = FEATURE_SPECS[view]
        entity_key = str(spec["entity_key"])
        entities = [entity for entity, _ in values]
        entity_df = pd.DataFrame({
            entity_key: entities,
            "event_timestamp": [timestamp for _, timestamp in values],
        })
        result = store.get_historical_features(
            entity_df=entity_df,
            features=[f"{view}:{feature}" for feature in spec["features"]],
            full_feature_names=True,
        ).to_df()
        if len(result) != len(values):
            raise RuntimeError(f"Feast returned {len(result)} {view} rows, expected {len(values)}")
        result = result.set_index(entity_key)
        source_root = str(store.get_feature_view(view, allow_registry_cache=False).batch_source.path)
        filesystem, persisted_root = url_to_fs(source_root)
        persisted_rows: list[tuple[dict[str, object], str]] = []
        for persisted_path in sorted(filesystem.glob(f"{persisted_root.rstrip('/')}/*.parquet")):
            with filesystem.open(persisted_path, "rb") as stream:
                for persisted in pq.read_table(stream).to_pylist():
                    persisted_rows.append((persisted, filesystem.unstrip_protocol(persisted_path)))
        for entity, timestamp in values:
            if entity not in result.index:
                raise RuntimeError(f"Feast did not return {view} entity {entity}")
            item = result.loc[entity]
            if getattr(item, "ndim", 1) != 1:
                raise RuntimeError(f"Feast returned duplicate {view} entity {entity}")
            row: dict[str, object] = {}
            for feature in spec["features"]:
                value = item[f"{view}__{feature}"]
                if pd.isna(value):
                    value = None
                elif hasattr(value, "item"):
                    value = value.item()
                row[feature] = value
            eligible = [
                (source, uri, source_time)
                for source, uri in persisted_rows
                if source.get(entity_key) == entity
                and (source_time := _online_time(source.get("event_timestamp"))) is not None
                and source_time <= timestamp
            ]
            eligible.sort(key=lambda candidate: (candidate[2], candidate[1]))
            row["__query_ts"] = timestamp
            if not eligible:
                row["__ts"] = None
                row["__pit_status"] = "MISSING"
                row["__persisted_uri"] = None
                row["__eligible_rows"] = 0
                row["__matching_value_rows"] = 0
                rows[(view, entity)] = row
                continue
            source, persisted_uri, source_time = eligible[-1]
            matching = [
                candidate
                for candidate in eligible
                if all(
                    (
                        row[feature] == candidate[0].get(feature)
                        or (
                            row[feature] is None
                            and candidate[0].get(feature) is None
                        )
                    )
                    for feature in spec["features"]
                )
            ]
            expected_matches = all(
                row[feature] == source.get(feature)
                or (row[feature] is None and source.get(feature) is None)
                for feature in spec["features"]
            )
            if not expected_matches:
                pit_status = "MISMATCH"
            elif len(matching) > 1:
                pit_status = "AMBIGUOUS_IDENTICAL_VALUES"
            else:
                pit_status = "PIT_VALUES_VERIFIED"
            row["__ts"] = source_time
            row["__pit_status"] = pit_status
            row["__persisted_uri"] = persisted_uri
            row["__eligible_rows"] = len(eligible)
            row["__matching_value_rows"] = len(matching)
            row["__selected_source_metadata_exposed_by_feast"] = False
            rows[(view, entity)] = row
    return rows


def compare_persisted_offline_online(
    offline: Mapping[tuple[str, str], Mapping[str, object]],
    online: Mapping[tuple[str, str], Mapping[str, object]],
) -> dict[str, object]:
    """Compare genuine Feast Offline retrieval with Feast Online rows."""

    failures: list[dict[str, object]] = []
    comparisons = 0
    mismatch_count = 0
    offline_keys = set(offline)
    online_keys = set(online)
    for key in sorted(offline_keys ^ online_keys):
        mismatch_count += 1
        failures.append({
            "key": list(key),
            "feature": "__entity__",
            "status": "MISSING_OFFLINE" if key not in offline else "MISSING_ONLINE",
        })
    for key in sorted(offline_keys & online_keys):
        view = key[0]
        offline_row = offline[key]
        online_row = online[key]
        offline_time = _online_time(offline_row.get("__ts"))
        online_time = _online_time(online_row.get("__ts"))
        pit_status = str(offline_row.get("__pit_status", "NOT_VERIFIED"))
        if pit_status not in {"EXACT", "PIT_VALUES_VERIFIED"}:
            mismatch_count += 1
            failures.append({
                "key": list(key),
                "feature": "__pit_status",
                "status": pit_status,
            })
            continue
        if offline_time is not None and online_time is not None and online_time > offline_time:
            mismatch_count += 1
            failures.append({
                "key": list(key),
                "feature": "__ts",
                "status": "ONLINE_NEWER_THAN_OFFLINE_CUTOVER",
                "offline": offline_time.isoformat() if offline_time else None,
                "online": online_time.isoformat() if online_time else None,
            })
            continue
        if offline_time is None or online_time is None or offline_time != online_time:
            mismatch_count += 1
            failures.append({
                "key": list(key),
                "feature": "__ts",
                "status": "TIMESTAMP_MISMATCH",
                "offline": offline_time.isoformat() if offline_time else None,
                "online": online_time.isoformat() if online_time else None,
            })
        for feature in FEATURE_SPECS[view]["features"]:
            comparisons += 1
            result = compare_feature(
                offline_row.get(feature),
                online_row.get(feature),
                offline_time,
                online_time,
                Ownership.HYBRID,
            )
            if result.status is not ParityStatus.MATCH:
                mismatch_count += 1
                failures.append({
                    "key": list(key),
                    "feature": feature,
                    "status": result.status.value,
                    "offline": offline_row.get(feature),
                    "online": online_row.get(feature),
                })
    return {
        "status": "PASS" if comparisons > 0 and mismatch_count == 0 else "FAIL",
        "entities_compared": len(offline_keys & online_keys),
        "comparisons": comparisons,
        "mismatch_count": mismatch_count,
        "failures": failures,
    }


def validate_offline_identity(root: str, campaign_id: str, run_id: str) -> None:
    """Require the Feast Offline root to identify the same campaign and run."""

    suffix = f"/campaigns/{campaign_id}/runs/{run_id}/offline"
    if not root.rstrip("/").endswith(suffix):
        raise ValueError(
            "offline feature root does not match the requested campaign and run"
        )
