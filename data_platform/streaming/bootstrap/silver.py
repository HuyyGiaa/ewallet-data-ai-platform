"""Deterministic first-deployment bootstrap from immutable Silver history.

The loader is intentionally read-only and bounded to the configured historical
horizon.  It feeds the same downstream managed-state operators used by normal
streaming while online writes remain disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from itertools import groupby
from typing import Iterable, Mapping
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

from data_platform.streaming.contracts.events import TransactionEvent


UTC = timezone.utc

SILVER_TRANSACTION_FIELDS = frozenset(
    {
        "transaction_id",
        "account_id",
        "user_id",
        "device_id",
        "type",
        "amount",
        "currency",
        "status",
        "channel",
        "old_balance",
        "new_balance",
        "merchant_id",
        "counterparty_account_id",
        "timestamp",
        "ingested_at",
    }
)
SILVER_DEVICE_FIELDS = frozenset({"device_id", "first_seen_at"})


class BootstrapMode(str, Enum):
    FIRST_DEPLOY_BOOTSTRAP = "FIRST_DEPLOY_BOOTSTRAP"
    NORMAL_STREAMING = "NORMAL_STREAMING"
    RESTART_RECOVERY = "RESTART_RECOVERY"


@dataclass(frozen=True)
class RuntimeFlow:
    mode: BootstrapMode
    source: str
    destination: str
    accepted_topic_role: str


RUNTIME_FLOWS = {
    BootstrapMode.FIRST_DEPLOY_BOOTSTRAP: RuntimeFlow(
        BootstrapMode.FIRST_DEPLOY_BOOTSTRAP,
        "immutable run-scoped Silver transactions + devices",
        "Flink keyed/window state with online writes disabled",
        "optional reconciliation only",
    ),
    BootstrapMode.NORMAL_STREAMING: RuntimeFlow(
        BootstrapMode.NORMAL_STREAMING,
        "ewallet.transactions.raw.v1",
        "Flink keyed/window state and online updates",
        "immutable historical sink input",
    ),
    BootstrapMode.RESTART_RECOVERY: RuntimeFlow(
        BootstrapMode.RESTART_RECOVERY,
        "Flink checkpoint/savepoint + retained ewallet.transactions.raw.v1",
        "restored Flink keyed/window state",
        "optional reconciliation only",
    ),
}


def _timestamp(value: object, field: str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime):
        raise ValueError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class CanonicalSilverBootstrapSource:
    """Immutable inputs needed to reconstruct all 31 historical states."""

    silver_root: str
    source_run_id: str
    dataset_version: str
    mode: BootstrapMode = BootstrapMode.FIRST_DEPLOY_BOOTSTRAP
    source_kind: str = "immutable_silver"
    manifest_uri: str | None = None

    def __post_init__(self) -> None:
        root = self.silver_root.rstrip("/")
        parsed = urlparse(root)
        if parsed.scheme != "gs" or not parsed.netloc:
            raise ValueError("canonical Silver bootstrap requires gs:// URI")
        expected = f"/lakehouse/runs/{self.source_run_id}/silver"
        if not root.endswith(expected):
            raise ValueError("silver_root must be immutable and run-scoped")
        if not self.dataset_version:
            raise ValueError("dataset_version is required")
        if self.mode is not BootstrapMode.FIRST_DEPLOY_BOOTSTRAP:
            raise ValueError("Silver bootstrap source is only valid for first deployment")
        manifest = urlparse(self.manifest_uri or "")
        if manifest.scheme != "gs" or not manifest.netloc or not manifest.path.strip("/"):
            raise ValueError("canonical bootstrap requires a gs:// provenance manifest")
        object.__setattr__(self, "silver_root", root)

    @property
    def transactions_uri(self) -> str:
        return f"{self.silver_root}/transactions"

    @property
    def devices_uri(self) -> str:
        return f"{self.silver_root}/devices"


class SilverBootstrapAdapter:
    """Map deduplicated Silver rows to the canonical streaming event contract."""

    def __init__(self, source: CanonicalSilverBootstrapSource) -> None:
        self.source = source

    def to_event(self, row: Mapping[str, object]) -> TransactionEvent:
        missing = SILVER_TRANSACTION_FIELDS - set(row)
        if missing:
            raise ValueError(f"Silver transaction row missing fields: {sorted(missing)}")
        transaction_id = str(row["transaction_id"])
        identity = (
            f"{self.source.dataset_version}/{self.source.source_run_id}/"
            f"{transaction_id}"
        )
        return TransactionEvent(
            event_id=str(uuid5(NAMESPACE_URL, f"bootstrap-event/{identity}")),
            transaction_id=transaction_id,
            user_id=str(row["user_id"]),
            account_id=str(row["account_id"]),
            device_id=str(row["device_id"]),
            merchant_id=(str(row["merchant_id"]) if row["merchant_id"] is not None else None),
            counterparty_account_id=(
                str(row["counterparty_account_id"])
                if row["counterparty_account_id"] is not None
                else None
            ),
            transaction_type=str(row["type"]),
            amount=float(row["amount"]),
            currency=str(row["currency"]),
            status=str(row["status"]),
            channel=str(row["channel"]),
            old_balance=float(row["old_balance"]),
            new_balance=float(row["new_balance"]),
            event_time=_timestamp(row["timestamp"], "timestamp"),
            arrival_time=_timestamp(row["ingested_at"], "ingested_at"),
            schema_version="1.0",
            trace_id=str(uuid5(NAMESPACE_URL, f"bootstrap-trace/{identity}")),
        )

    def ordered_event_groups(
        self, rows: Iterable[Mapping[str, object]]
    ) -> list[list[TransactionEvent]]:
        events = sorted(
            (self.to_event(row) for row in rows),
            key=lambda event: (event.event_time, event.transaction_id),
        )
        return [list(group) for _, group in groupby(events, key=lambda event: event.event_time)]

    @staticmethod
    def device_first_seen(rows: Iterable[Mapping[str, object]]) -> dict[str, datetime]:
        values: dict[str, datetime] = {}
        for row in rows:
            missing = SILVER_DEVICE_FIELDS - set(row)
            if missing:
                raise ValueError(f"Silver device row missing fields: {sorted(missing)}")
            device_id = str(row["device_id"])
            seen = _timestamp(row["first_seen_at"], "first_seen_at")
            existing = values.get(device_id)
            if existing is not None and existing != seen:
                raise ValueError(f"conflicting first_seen_at for {device_id}")
            values[device_id] = seen
        return values

    def account_anchors(
        self,
        rows: Iterable[Mapping[str, object]],
        replay_start: datetime,
    ) -> dict[str, datetime]:
        start = _timestamp(replay_start, "replay_start")
        anchors: dict[str, datetime] = {}
        for row in rows:
            event = self.to_event(row)
            if event.event_time >= start:
                continue
            previous = anchors.get(event.account_id)
            if previous is None or event.event_time > previous:
                anchors[event.account_id] = event.event_time
        return anchors


@dataclass(frozen=True)
class BootstrapProvenance:
    dataset_version: str
    source_run_id: str
    silver_root: str
    status: str

    @classmethod
    def validate(
        cls, payload: Mapping[str, object], source: CanonicalSilverBootstrapSource
    ) -> "BootstrapProvenance":
        provenance = cls(
            dataset_version=str(payload.get("dataset_version", "")),
            source_run_id=str(payload.get("source_run_id") or payload.get("silver_run_id") or ""),
            silver_root=str(payload.get("silver_root", "")).rstrip("/"),
            status=str(payload.get("status", "")),
        )
        if provenance.dataset_version != source.dataset_version:
            raise ValueError("bootstrap manifest dataset identity mismatch")
        if provenance.source_run_id != source.source_run_id:
            raise ValueError("bootstrap manifest run identity mismatch")
        if provenance.silver_root != source.silver_root:
            raise ValueError("bootstrap manifest Silver root mismatch")
        if provenance.status not in {"COMPLETE", "PASS"}:
            raise ValueError("bootstrap provenance is not PASS/COMPLETE")
        return provenance


class DeltaSilverBootstrapReader:
    """Concrete Delta/GCS reader; credentials are supplied by the runtime."""

    def __init__(self, source: CanonicalSilverBootstrapSource, storage_options=None) -> None:
        self.source = source
        self.storage_options = dict(storage_options or {})
        self.adapter = SilverBootstrapAdapter(source)

    def _table(self, uri: str):
        from deltalake import DeltaTable

        return DeltaTable(uri, storage_options=self.storage_options)

    def transaction_rows(self, replay_start: datetime, cutover: datetime):
        start = _timestamp(replay_start, "replay_start")
        end = _timestamp(cutover, "cutover")
        import pyarrow.dataset as ds

        dataset = self._table(self.source.transactions_uri).to_pyarrow_dataset()
        missing = SILVER_TRANSACTION_FIELDS - set(dataset.schema.names)
        if missing:
            raise ValueError(f"Silver transactions missing fields: {sorted(missing)}")
        scanner = dataset.scanner(
            columns=sorted(SILVER_TRANSACTION_FIELDS),
            filter=(ds.field("timestamp") >= start) & (ds.field("timestamp") < end),
            batch_size=10_000,
        )
        for batch in scanner.to_batches():
            yield from batch.to_pylist()

    def pre_window_rows(self, replay_start: datetime):
        start = _timestamp(replay_start, "replay_start")
        import pyarrow.dataset as ds

        scanner = self._table(self.source.transactions_uri).to_pyarrow_dataset().scanner(
            columns=sorted(SILVER_TRANSACTION_FIELDS),
            filter=ds.field("timestamp") < start,
            batch_size=10_000,
        )
        for batch in scanner.to_batches():
            yield from batch.to_pylist()

    def device_rows(self):
        dataset = self._table(self.source.devices_uri).to_pyarrow_dataset()
        missing = SILVER_DEVICE_FIELDS - set(dataset.schema.names)
        if missing:
            raise ValueError(f"Silver devices missing fields: {sorted(missing)}")
        for batch in dataset.scanner(columns=sorted(SILVER_DEVICE_FIELDS), batch_size=10_000).to_batches():
            yield from batch.to_pylist()

    def replay_events(self, replay_start: datetime, cutover: datetime):
        """Yield deterministic replay records; callers must keep online disabled."""
        rows = self.transaction_rows(replay_start, cutover)
        for group in self.adapter.ordered_event_groups(rows):
            for event in group:
                yield event
