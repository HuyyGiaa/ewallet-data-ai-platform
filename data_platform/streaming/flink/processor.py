"""Composition root for deterministic Block A operator semantics.

The runtime Flink adapter must call this logic only for timestamp groups that
the watermark has finalized. That preserves same-timestamp snapshots and lets
late-but-accepted events participate before a group is emitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.streaming.config.settings import FlinkProfile
from data_platform.streaming.contracts.events import TransactionEvent
from data_platform.streaming.flink.dedup import DedupState
from data_platform.streaming.flink.event_time import EventDisposition, EventTimeTracker
from data_platform.streaming.flink.feature_state import HistoricalFeatureState
from data_platform.streaming.observability.metrics import BlockAMetrics
from data_platform.streaming.reconciliation.late import LateEventEnvelope
from data_platform.streaming.retry.routing import FailureEnvelope
from data_platform.streaming.sinks.historical import HistoricalRecord
from data_platform.streaming.sinks.online import OnlineFeatureUpdate


@dataclass
class ProcessingResult:
    historical_records: list[HistoricalRecord] = field(default_factory=list)
    online_updates: list[OnlineFeatureUpdate] = field(default_factory=list)
    duplicate_transaction_ids: list[str] = field(default_factory=list)
    dlq_records: list[FailureEnvelope] = field(default_factory=list)
    late_records: list[LateEventEnvelope] = field(default_factory=list)


class BlockAProcessor:
    def __init__(
        self,
        profile: FlinkProfile,
        feature_state: HistoricalFeatureState | None = None,
    ) -> None:
        self.profile = profile
        self.event_time = EventTimeTracker(profile.event_time)
        self.dedup = DedupState(profile.dedup_ttl)
        self.features = feature_state or HistoricalFeatureState(
            history_ttl=profile.history_ttl
        )
        self.metrics = BlockAMetrics()

    def process_group(self, events: list[TransactionEvent]) -> ProcessingResult:
        if events and len({event.event_time for event in events}) != 1:
            raise ValueError("Flink timer must finalize exactly one timestamp group")
        result = ProcessingResult()
        eligible: list[TransactionEvent] = []
        for event in events:
            self.metrics.increment("input_events_total")
            if self.dedup.is_duplicate(event.transaction_id, event.arrival_time):
                result.duplicate_transaction_ids.append(event.transaction_id)
                self.metrics.increment("duplicates_detected_total")
                self.metrics.increment("duplicates_removed_total")
                continue
            disposition = self.event_time.classify(event)
            result.historical_records.append(
                HistoricalRecord.from_event(event, disposition.value)
            )
            self.metrics.increment("historical_records_total")
            if disposition == EventDisposition.TOO_LATE:
                self.metrics.increment("too_late_total")
                result.late_records.append(
                    LateEventEnvelope.from_event(
                        event,
                        observed_watermark=self.event_time.watermark,
                        reason="event arrived after watermark plus allowed lateness",
                    )
                )
                self.metrics.increment("late_reconciliation_total")
            else:
                eligible.append(event)
                metric = (
                    "late_accepted_total"
                    if disposition == EventDisposition.LATE_ACCEPTED
                    else "on_time_total"
                )
                self.metrics.increment(metric)

        snapshots = self.features.snapshot_group(eligible)
        for event in eligible:
            for view_name, snapshot in snapshots[event.transaction_id].items():
                spec = FEATURE_SPECS[view_name]
                result.online_updates.append(
                    OnlineFeatureUpdate(
                        view_name=view_name,
                        entity_name=str(spec["entity_name"]),
                        entity_key=str(spec["entity_key"]),
                        entity_value=snapshot.entity_value,
                        feature_timestamp=snapshot.feature_timestamp,
                        values=snapshot.values,
                        source_event_id=event.event_id,
                    )
                )
                self.metrics.increment("online_updates_total")
        for event in events:
            self.event_time.observe(event)
        self.metrics.increment("output_events_total", len(eligible))
        return result
