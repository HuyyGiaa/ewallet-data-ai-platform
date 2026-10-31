"""Production PyFlink operators backed only by Flink managed state.

The dependency-free processor remains the semantic oracle.  These operators
are the deployment path and deliberately do not retain module-global state.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from pyflink.common import Types
from pyflink.common.time import Time
from pyflink.datastream.functions import FlatMapFunction, KeyedProcessFunction, MapFunction
from pyflink.datastream.state import MapStateDescriptor, StateTtlConfig, ValueStateDescriptor

from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.streaming.contracts.events import TransactionEvent
from data_platform.streaming.contracts.topics import entity_rekey
from data_platform.streaming.flink.records import EntityEvent, OutboundRecord, RuntimeEvent
from data_platform.streaming.reconciliation.late import LateEventEnvelope
from data_platform.streaming.retry.routing import FailureEnvelope
from data_platform.streaming.sinks.historical import HistoricalRecord
from data_platform.streaming.sinks.online import OnlineFeatureUpdate

UTC = timezone.utc


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


class BootstrapContractError(ValueError):
    """Bootstrap identity or payload is incompatible with the declared replay."""


class BootstrapCutoverError(BootstrapContractError):
    """Live/retry traffic entered the bootstrap-only source before cutover."""


class ParseAndValidate(MapFunction):
    """DLQ ordinary invalid records and fail closed on bootstrap violations."""

    def __init__(
        self,
        expected_dataset_version=None,
        expected_source_run_id=None,
        expected_cutover=None,
        bootstrap_only: bool = False,
    ):
        self.expected_dataset_version = expected_dataset_version
        self.expected_source_run_id = expected_source_run_id
        self.expected_cutover = expected_cutover
        self.bootstrap_only = bootstrap_only

    def _validate_bootstrap_identity(self, raw) -> None:
        expected = {
            "dataset_version": self.expected_dataset_version,
            "source_run_id": self.expected_source_run_id,
            "cutover": self.expected_cutover,
        }
        for field, value in expected.items():
            if value is not None and raw.get(field) != value:
                raise BootstrapContractError(f"bootstrap {field} mismatch")

    def open(self, runtime_context) -> None:
        group = runtime_context.get_metric_group()
        self.inputs = group.counter("input_events_total")
        self.invalid = group.counter("dlq_total")

    def map(self, payload: str) -> str:
        self.inputs.inc()
        raw = {}
        try:
            raw = json.loads(payload)
        except Exception as error:
            if self.bootstrap_only:
                raise BootstrapContractError(
                    "FIRST_DEPLOY_BOOTSTRAP received malformed JSON; "
                    "bootstrap provenance cannot be verified"
                ) from error
            return self._to_dlq(payload, raw, error)

        kind = raw.get("kind") if isinstance(raw, dict) else None
        if kind == "BOOTSTRAP_COMPLETE":
            self._validate_bootstrap_identity(raw)
            return RuntimeEvent(
                None,
                "BOOTSTRAP_COMPLETE",
                reason=payload,
                bootstrap=True,
                online_enabled=False,
            ).to_json()
        if kind == "BOOTSTRAP":
            self._validate_bootstrap_identity(raw)
            try:
                return RuntimeEvent(
                    TransactionEvent.from_dict(raw["event"]),
                    "VALID",
                    bootstrap=True,
                    online_enabled=False,
                    device_first_seen_iso=raw.get("device_first_seen_iso"),
                    account_anchor_iso=raw.get("account_anchor_iso"),
                ).to_json()
            except Exception as error:
                raise BootstrapContractError("invalid bootstrap event payload") from error

        if self.bootstrap_only:
            raise BootstrapCutoverError(
                "FIRST_DEPLOY_BOOTSTRAP received a live/retry event; "
                "stop bootstrap without committing source offsets"
            )

        try:
            if "original_event" in raw and "retry_count" in raw:
                failure = FailureEnvelope.from_json(payload)
                event = TransactionEvent.from_dict(failure.original_event)
                return RuntimeEvent(event, "VALID", failure.retry_count).to_json()
            return RuntimeEvent(TransactionEvent.from_dict(raw), "VALID").to_json()
        except Exception as error:  # record-level validation boundary
            return self._to_dlq(payload, raw, error)

    def _to_dlq(self, payload: str, raw, error: Exception) -> str:
        self.invalid.inc()
        retry_count = int(raw.get("retry_count", 0)) if isinstance(raw, dict) else 0
        return RuntimeEvent(
            None,
            "DLQ",
            retry_count=retry_count,
            reason=str(error),
            raw_payload=payload,
        ).to_json()


class ManagedDeduplicate(KeyedProcessFunction):
    """48-hour transaction identity dedup in Flink ValueState."""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds

    def open(self, runtime_context) -> None:
        descriptor = ValueStateDescriptor("transaction-seen", Types.BOOLEAN())
        ttl = (
            StateTtlConfig.new_builder(Time.seconds(self.ttl_seconds))
            .update_ttl_on_create_and_write()
            .never_return_expired()
            .build()
        )
        descriptor.enable_time_to_live(ttl)
        self.seen = runtime_context.get_state(descriptor)
        group = runtime_context.get_metric_group()
        self.duplicates = group.counter("duplicates_detected_total")
        self.removed = group.counter("duplicates_removed_total")
        self.unique = group.counter("output_events_total")

    def process_element(self, value: str, ctx):
        record = RuntimeEvent.from_json(value)
        if record.bootstrap or record.retry_count > 0:
            yield value
            return
        if self.seen.value():
            self.duplicates.inc()
            self.removed.inc()
            yield RuntimeEvent(record.event, "DUPLICATE", record.retry_count).to_json()
            return
        self.seen.update(True)
        self.unique.inc()
        yield value


class EventTimeDisposition(KeyedProcessFunction):
    """Classify against the runtime watermark; TOO_LATE never reaches state."""

    def __init__(self, allowed_lateness_ms: int) -> None:
        self.allowed_lateness_ms = allowed_lateness_ms

    def open(self, runtime_context) -> None:
        group = runtime_context.get_metric_group()
        self.on_time = group.counter("on_time_total")
        self.late = group.counter("late_accepted_total")
        self.too_late = group.counter("too_late_total")

    def process_element(self, value: str, ctx):
        record = RuntimeEvent.from_json(value)
        event = record.event
        watermark = ctx.timer_service().current_watermark()
        timestamp = _ms(event.event_time)
        if watermark < 0 or timestamp >= watermark:
            disposition = "ON_TIME"
            self.on_time.inc()
        elif timestamp >= watermark - self.allowed_lateness_ms:
            disposition = "LATE_ACCEPTED"
            self.late.inc()
        else:
            disposition = "TOO_LATE"
            self.too_late.inc()
        yield RuntimeEvent(
            event,
            "TOO_LATE" if disposition == "TOO_LATE" else "ELIGIBLE",
            record.retry_count,
            disposition=disposition,
            bootstrap=record.bootstrap,
            online_enabled=record.online_enabled,
            device_first_seen_iso=record.device_first_seen_iso,
            account_anchor_iso=record.account_anchor_iso,
        ).to_json()


class EntityFanOut(FlatMapFunction):
    def flat_map(self, value: str):
        record = RuntimeEvent.from_json(value)
        for view_name, state_key in entity_rekey(record.event).items():
            entity_key = str(FEATURE_SPECS[view_name]["entity_key"])
            yield EntityEvent(
                view_name=view_name,
                entity_key=entity_key,
                entity_value=state_key.split(":", 1)[1],
                event=record.event,
                disposition=record.disposition,
                bootstrap=record.bootstrap,
                online_enabled=record.online_enabled,
                retry_count=record.retry_count,
                device_first_seen_iso=record.device_first_seen_iso,
                account_anchor_iso=record.account_anchor_iso,
            ).to_json()


class BootstrapCompletionMetric(MapFunction):
    def open(self, runtime_context) -> None:
        self.complete = runtime_context.get_metric_group().counter("bootstrap_complete_total")

    def map(self, value: str) -> str:
        if RuntimeEvent.from_json(value).route == "BOOTSTRAP_COMPLETE":
            self.complete.inc()
        return value


def _window(events, at_ms: int, duration_ms: int):
    lower = at_ms - duration_ms
    return [event for event in events if lower <= _ms(event.event_time) < at_ms]


def _rate(events):
    return None if not events else sum(e.status == "failed" for e in events) / len(events)


def _std(values):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


class ManagedHistoricalFeatures(KeyedProcessFunction):
    """Timestamp-grouped [T-window,T) snapshots for all four FeatureViews."""

    def __init__(self, allowed_lateness_ms: int, history_ttl_ms: int) -> None:
        self.allowed_lateness_ms = allowed_lateness_ms
        self.history_ttl_ms = history_ttl_ms

    def open(self, runtime_context) -> None:
        self.events = runtime_context.get_map_state(
            MapStateDescriptor("history-by-time-and-id", Types.STRING(), Types.STRING())
        )
        self.pending = runtime_context.get_map_state(
            MapStateDescriptor("pending-timestamp-groups", Types.STRING(), Types.BOOLEAN())
        )
        self.device_first_seen = runtime_context.get_state(
            ValueStateDescriptor("device-first-seen-ms", Types.LONG())
        )
        self.account_anchor = runtime_context.get_state(
            ValueStateDescriptor("account-pre-window-anchor-ms", Types.LONG())
        )
        self.updates = runtime_context.get_metric_group().counter("online_updates_total")
        self.retries = runtime_context.get_metric_group().counter("retry_total")
        self.dlq = runtime_context.get_metric_group().counter("dlq_total")

    def process_element(self, value: str, ctx):
        item = EntityEvent.from_json(value)
        event_ms = _ms(item.event.event_time)
        key = f"{event_ms:020d}:{item.event.transaction_id}"
        self.events.put(key, value)
        if item.view_name == "device_behavior":
            candidate = _ms(datetime.fromisoformat(item.device_first_seen_iso)) if item.device_first_seen_iso else event_ms
            current = self.device_first_seen.value()
            if current is None or candidate < current:
                self.device_first_seen.update(candidate)
        if item.account_anchor_iso:
            self.account_anchor.update(_ms(datetime.fromisoformat(item.account_anchor_iso)))
        timer = event_ms + self.allowed_lateness_ms + 1
        if not self.pending.contains(str(event_ms)):
            self.pending.put(str(event_ms), True)
            ctx.timer_service().register_event_time_timer(timer)

    def on_timer(self, timestamp: int, ctx):
        at_ms = timestamp - self.allowed_lateness_ms - 1
        group = []
        history = []
        for key, raw in self.events.items():
            item = EntityEvent.from_json(raw)
            current_ms = _ms(item.event.event_time)
            if current_ms == at_ms:
                group.append(item)
            elif current_ms < at_ms:
                history.append(item.event)
        group.sort(key=lambda item: item.event.transaction_id)
        for item in group:
            try:
                values = self._values(item, history, at_ms)
                if item.online_enabled and not item.bootstrap:
                    spec = FEATURE_SPECS[item.view_name]
                    update = OnlineFeatureUpdate(
                        item.view_name,
                        str(spec["entity_name"]),
                        item.entity_key,
                        item.entity_value,
                        item.event.event_time,
                        values,
                        item.event.event_id,
                    )
                    self.updates.inc()
                    yield OutboundRecord("online", update.message_key, update.to_json()).to_json()
            except (ValueError, TypeError, ArithmeticError) as error:
                next_count = item.retry_count + 1
                destination = "retry" if next_count <= 3 else "dlq"
                (self.retries if destination == "retry" else self.dlq).inc()
                failure = FailureEnvelope(item.event.to_dict(), item.event.event_time.isoformat(), next_count, str(error), item.event.trace_id)
                yield OutboundRecord(destination, item.event.transaction_id, failure.to_json()).to_json()
        cutoff = at_ms - self.history_ttl_ms
        for key in list(self.events.keys()):
            if int(key.split(":", 1)[0]) < cutoff:
                if group and group[0].view_name == "account_behavior":
                    evicted_ms = int(key.split(":", 1)[0])
                    anchor = self.account_anchor.value()
                    if anchor is None or evicted_ms > anchor:
                        self.account_anchor.update(evicted_ms)
                self.events.remove(key)
        self.pending.remove(str(at_ms))

    def _values(self, item: EntityEvent, history, at_ms: int):
        view = item.view_name
        w5 = _window(history, at_ms, 300_000)
        w10 = _window(history, at_ms, 600_000)
        w1 = _window(history, at_ms, 3_600_000)
        w24 = _window(history, at_ms, 86_400_000)
        w30 = _window(history, at_ms, 2_592_000_000)
        amounts30 = [event.amount for event in w30]
        if view == "user_behavior":
            return {"user_tx_count_5m": len(w5), "user_tx_count_1h": len(w1), "user_tx_count_24h": len(w24), "user_amount_sum_1h": sum(e.amount for e in w1), "user_amount_observation_count_30d": len(amounts30), "user_avg_amount_30d": sum(amounts30) / len(amounts30) if amounts30 else None, "user_std_amount_30d": _std(amounts30), "user_failed_rate_24h": _rate(w24), "user_distinct_merchants_24h": len({e.merchant_id for e in w24 if e.merchant_id})}
        if view == "account_behavior":
            previous = [(_ms(event.event_time)) for event in history]
            anchor = self.account_anchor.value()
            if anchor is not None and anchor < at_ms:
                previous.append(anchor)
            return {"account_tx_count_5m": len(w5), "account_tx_count_1h": len(w1), "account_tx_count_24h": len(w24), "account_amount_sum_1h": sum(e.amount for e in w1), "account_amount_sum_24h": sum(e.amount for e in w24), "account_amount_observation_count_30d": len(amounts30), "account_avg_amount_30d": sum(amounts30) / len(amounts30) if amounts30 else None, "account_std_amount_30d": _std(amounts30), "account_failed_rate_24h": _rate(w24), "account_seconds_since_last_tx": (at_ms - max(previous)) / 1000 if previous else None}
        if view == "device_behavior":
            first = self.device_first_seen.value()
            return {"device_age_seconds": max(0.0, (at_ms - first) / 1000) if first is not None else 0.0, "device_tx_count_1h": len(w1), "device_tx_count_24h": len(w24), "device_amount_sum_24h": sum(e.amount for e in w24), "device_failed_rate_24h": _rate(w24)}
        return {"merchant_tx_count_10m": len(w10), "merchant_tx_count_1h": len(w1), "merchant_tx_count_24h": len(w24), "merchant_unique_users_10m": len({e.user_id for e in w10}), "merchant_unique_users_1h": len({e.user_id for e in w1}), "merchant_amount_sum_1h": sum(e.amount for e in w1), "merchant_avg_amount_24h": sum(e.amount for e in w24) / len(w24) if w24 else None}


class HistoricalJson(MapFunction):
    def open(self, runtime_context) -> None:
        self.records = runtime_context.get_metric_group().counter("historical_records_total")

    def map(self, value: str) -> str:
        self.records.inc()
        item = RuntimeEvent.from_json(value)
        payload = HistoricalRecord.from_event(item.event, item.disposition).to_json()
        return OutboundRecord("accepted", item.event.transaction_id, payload).to_json()


class LateJson(MapFunction):
    def open(self, runtime_context) -> None:
        self.records = runtime_context.get_metric_group().counter("late_reconciliation_total")

    def map(self, value: str) -> str:
        self.records.inc()
        item = RuntimeEvent.from_json(value)
        payload = json.dumps(
            LateEventEnvelope.from_event(item.event, None, "past watermark plus allowed lateness").__dict__,
            default=str,
            sort_keys=True,
            separators=(",", ":"),
        )
        return OutboundRecord("late", item.event.transaction_id, payload).to_json()


class InvalidJson(MapFunction):
    def map(self, value: str) -> str:
        item = RuntimeEvent.from_json(value)
        try:
            raw = json.loads(item.raw_payload)
        except Exception:
            raw = {"raw_payload": item.raw_payload}
        envelope = FailureEnvelope(
            original_event=raw,
            original_event_time=str(raw.get("event_time", "")),
            retry_count=item.retry_count,
            error_reason=item.reason,
            trace_id=str(raw.get("trace_id", "")),
        )
        key = str(raw.get("transaction_id") or raw.get("event_id") or "invalid")
        return OutboundRecord("dlq", key, envelope.to_json()).to_json()
