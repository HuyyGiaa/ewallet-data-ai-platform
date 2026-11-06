"""Scenario, offline/online comparison and sink service behaviour for Block A.

Everything here is local and deterministic. Passing these tests shows the logic is
correct; it does not show that a deployed Flink job wrote to either store.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.streaming.config.settings import (
    BASELINE,
    OperatingMode,
    RuntimeSettings,
)
from data_platform.streaming.contracts.events import TransactionEvent
from data_platform.streaming.contracts.features import Ownership
from data_platform.streaming.feast.parity import ParityStatus, compare_feature
from data_platform.streaming.feast.service import (
    dead_letter_envelope,
    parse_update,
    run_online_writer,
)
from data_platform.streaming.flink.job import source_topic_names, uses_source_idleness
from data_platform.streaming.messaging.consumer_loop import StopSignal, poll_messages
from data_platform.streaming.retry.routing import FailureEnvelope
from data_platform.streaming.sinks.historical import HistoricalRecord
from data_platform.streaming.sinks.historical_service import (
    HistoryBatcher,
    ImmutableGcsBatchWriter,
    ImmutableLocalBatchWriter,
    history_writer,
    run_history_sink,
)
from data_platform.streaming.sinks.online import OnlineFeatureUpdate
from data_platform.streaming.verification.consistency import (
    HistoryRecord,
    build_report,
    check_dispositions,
    compare_online_state,
    expected_online_state,
    parse_history,
)
from data_platform.streaming.verification.scenario import (
    DEFAULT_START,
    _CLOSING_USER,
    build_scenario,
    namespace_seed,
)


UTC = timezone.utc
NAMESPACE = "verify-unit"


def scenario_events(scenario):
    """Decode scenario records, skipping the invalid payload."""
    events = []
    for phase in scenario.phases:
        for record in phase.records:
            try:
                events.append((phase.name, TransactionEvent.from_dict(json.loads(record))))
            except ValueError:
                continue
    return events


def history_from(scenario, repeat=()):
    """Simulate what Flink writes to the accepted topic for the scenario."""
    manifest = scenario.manifest()["expected"]
    disposition = {
        **{item: "ON_TIME" for item in manifest["on_time"]},
        **{item: "LATE_ACCEPTED" for item in manifest["late_accepted"]},
        **{item: "TOO_LATE" for item in manifest["too_late"]},
        **{item: "ON_TIME" for item in manifest["closing"]},
    }
    seen = set()
    lines = []
    for _, event in scenario_events(scenario):
        if event.transaction_id in seen:
            continue
        seen.add(event.transaction_id)
        line = HistoricalRecord.from_event(event, disposition[event.transaction_id]).to_json()
        lines.append(line)
        if event.transaction_id in repeat:
            lines.append(line)
    return lines


def online_copy(expected):
    """Build Feast-shaped rows that exactly match the expected state."""
    return {
        key: {**snapshot.values, "__ts": int(snapshot.feature_timestamp.timestamp())}
        for key, snapshot in expected.items()
    }


class FakeMessage:
    def __init__(self, value=b"{}", partition=0, offset=0, error=None):
        self._value, self._partition, self._offset, self._error = value, partition, offset, error

    def value(self):
        return self._value

    def partition(self):
        return self._partition

    def offset(self):
        return self._offset

    def error(self):
        return self._error


class FakeConsumer:
    """Replay a fixed list, then report an empty topic."""

    def __init__(self, messages=()):
        self.messages = list(messages)
        self.commits = []

    def poll(self, _timeout):
        return self.messages.pop(0) if self.messages else None

    def commit(self, message, asynchronous):
        self.commits.append(message)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1.0
        return self.now


class RecordingWriter:
    def __init__(self, failures=0):
        self.batches = []
        self.failures = failures

    def write(self, partition, first_offset, messages):
        if self.failures:
            self.failures -= 1
            raise OSError("store unavailable")
        self.batches.append((partition, first_offset, list(messages)))
        return f"object-{len(self.batches)}"


class RuntimeModeTest(unittest.TestCase):
    def settings(self, **overrides):
        values = dict(
            mode=OperatingMode.ISOLATED_VERIFICATION,
            profile=BASELINE,
            bootstrap_servers="redpanda:29092",
            kafka_connector_jar=Path("x.jar"),
            checkpoint_storage="file:///c",
            namespace=NAMESPACE,
        )
        return RuntimeSettings(**{**values, **overrides})

    def test_isolated_mode_needs_a_verify_namespace(self):
        self.settings()
        for namespace in (None, "bench-x", "prod", "verify-"):
            with self.subTest(namespace), self.assertRaises(ValueError):
                self.settings(namespace=namespace)

    def test_isolated_mode_refuses_bootstrap_inputs(self):
        with self.assertRaises(ValueError):
            self.settings(silver_root="gs://x")
        with self.assertRaises(ValueError):
            self.settings(cutover_iso="2026-06-01T00:00:00+00:00")

    def test_production_modes_still_refuse_a_namespace(self):
        with self.assertRaises(ValueError):
            self.settings(mode=OperatingMode.NORMAL_STREAMING, savepoint_path="gs://s")

    def test_isolated_mode_uses_the_production_event_time_path(self):
        self.assertTrue(uses_source_idleness(OperatingMode.ISOLATED_VERIFICATION))
        self.assertEqual(
            ("ewallet.transactions.raw.v1.verify-unit", "ewallet.transactions.retry.v1.verify-unit"),
            source_topic_names(OperatingMode.ISOLATED_VERIFICATION, NAMESPACE),
        )


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.scenario = build_scenario(NAMESPACE, event_time=BASELINE.event_time)
        self.events = scenario_events(self.scenario)
        self.expected = self.scenario.manifest()["expected"]

    def test_building_twice_is_identical(self):
        again = build_scenario(NAMESPACE, event_time=BASELINE.event_time)
        self.assertEqual(self.scenario.manifest(), again.manifest())

    def test_each_namespace_gets_its_own_entities(self):
        other = scenario_events(build_scenario("verify-other"))
        self.assertNotEqual(namespace_seed(NAMESPACE), namespace_seed("verify-other"))
        self.assertFalse({e.user_id for _, e in self.events} & {e.user_id for _, e in other})

    def test_events_follow_the_snapshot_end(self):
        self.assertGreaterEqual(
            min(event.event_time for name, event in self.events if name == "normal"), DEFAULT_START
        )

    def test_every_event_path_is_present(self):
        self.assertEqual(13, len(self.expected["on_time"]))
        for name in ("late_accepted", "too_late", "invalid_event_ids", "closing"):
            self.assertEqual(1, len(self.expected[name]))
        self.assertEqual(2, len(self.expected["duplicates"]))

    def test_duplicates_reuse_a_real_transaction(self):
        ids = [event.transaction_id for _, event in self.events]
        for transaction in self.expected["duplicates"]:
            self.assertEqual(2, ids.count(transaction))

    def event(self, phase):
        return next(event for name, event in self.events if name == phase)

    def test_lateness_matches_the_configured_watermark(self):
        newest = max(event.event_time for name, event in self.events if name == "normal")
        settings = BASELINE.event_time
        watermark = newest - settings.max_out_of_orderness
        self.assertGreaterEqual(self.event("out_of_order").event_time, watermark)
        late = self.event("late_accepted").event_time
        self.assertLess(late, watermark)
        self.assertGreaterEqual(late, watermark - settings.allowed_lateness)
        self.assertLess(self.event("too_late").event_time, watermark - settings.allowed_lateness)

    def test_the_closing_event_closes_every_window(self):
        settings = BASELINE.event_time
        newest = max(event.event_time for name, event in self.events if name == "normal")
        watermark = self.event("closing").event_time - settings.max_out_of_orderness
        self.assertGreater(watermark, newest + settings.allowed_lateness)

    def test_the_closing_event_shares_no_entity_with_the_scenario(self):
        closing = self.event("closing")
        others = [event for name, event in self.events if name != "closing"]
        self.assertNotIn(closing.user_id, {event.user_id for event in others})
        self.assertNotIn(closing.device_id, {event.device_id for event in others})
        self.assertNotIn(closing.account_id, {event.account_id for event in others})
        self.assertIsNone(closing.merchant_id)

    def test_one_user_is_reserved_for_closing(self):
        self.assertEqual(7, _CLOSING_USER)

    def test_sub_second_timestamps_are_present(self):
        self.assertTrue(any(event.event_time.microsecond for _, event in self.events))


class ConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.scenario = build_scenario(NAMESPACE, event_time=BASELINE.event_time)
        self.manifest = self.scenario.manifest()
        self.records, self.repeats = parse_history(history_from(self.scenario))
        self.expected = expected_online_state(self.records, self.manifest["expected"]["closing"])

    def report(self, online=None, records=None):
        records = records or self.records
        comparison = compare_online_state(self.expected, online_copy(self.expected) if online is None else online)
        return build_report(self.manifest, records, self.repeats, self.expected, comparison,
                            check_dispositions(records, self.manifest), {"run_id": "r"})

    def test_an_exact_online_copy_passes(self):
        report = self.report()
        self.assertEqual("PASS", report["status"])
        self.assertEqual(sorted(FEATURE_SPECS), report["feature_views"])
        self.assertGreater(report["features_compared"], 31)

    def test_repeated_history_is_dropped(self):
        target = self.manifest["expected"]["on_time"][0]
        records, repeats = parse_history(history_from(self.scenario, repeat={target}))
        self.assertEqual(1, repeats)
        self.assertEqual(len(self.records), len(records))

    def test_blank_history_lines_are_ignored(self):
        records, _ = parse_history(["", b"  ", *history_from(self.scenario)])
        self.assertEqual(len(self.records), len(records))

    def test_conflicting_dispositions_are_an_error(self):
        lines = history_from(self.scenario)
        value = json.loads(lines[0])
        value["disposition"] = "TOO_LATE"
        with self.assertRaisesRegex(ValueError, "conflicting"):
            parse_history([*lines, json.dumps(value)])

    def test_too_late_events_never_enter_state(self):
        too_late = self.manifest["expected"]["too_late"][0]
        kept = [record for record in self.records if record.event.transaction_id != too_late]
        self.assertEqual(self.expected, expected_online_state(kept, self.manifest["expected"]["closing"]))

    def test_late_accepted_events_enter_state(self):
        late = self.manifest["expected"]["late_accepted"][0]
        kept = [record for record in self.records if record.event.transaction_id != late]
        self.assertNotEqual(self.expected, expected_online_state(kept, self.manifest["expected"]["closing"]))

    def test_the_closing_event_is_excluded_from_expectations(self):
        closing = next(r.event for r in self.records
                       if r.event.transaction_id == self.manifest["expected"]["closing"][0])
        self.assertNotIn(("user_behavior", closing.user_id), self.expected)

    def test_a_wrong_value_fails(self):
        online = online_copy(self.expected)
        key = next(key for key in online if key[0] == "user_behavior")
        online[key]["user_tx_count_24h"] += 1
        report = self.report(online)
        self.assertEqual("FAIL", report["status"])
        self.assertEqual(1, report["comparison"]["counts"]["MISMATCH"])

    def test_a_missing_row_fails(self):
        online = online_copy(self.expected)
        online.pop(next(iter(online)))
        report = self.report(online)
        self.assertEqual("FAIL", report["status"])
        self.assertGreater(report["comparison"]["counts"]["MISSING"], 0)

    def test_a_stale_row_fails(self):
        online = online_copy(self.expected)
        key = next(iter(online))
        online[key]["__ts"] -= 3600
        report = self.report(online)
        self.assertEqual("FAIL", report["status"])
        self.assertEqual(1, report["comparison"]["timestamp_mismatches"])

    def test_a_small_timestamp_drift_is_caught(self):
        online = online_copy(self.expected)
        online[next(iter(online))]["__ts"] += 1
        self.assertEqual("FAIL", self.report(online)["status"])

    def test_an_empty_comparison_cannot_pass(self):
        self.expected = {}
        self.assertEqual("FAIL", self.report({})["status"])

    def test_a_misclassified_event_fails(self):
        records = [
            replace(record, disposition="ON_TIME")
            if record.event.transaction_id in self.manifest["expected"]["too_late"] else record
            for record in self.records
        ]
        report = self.report(records=records)
        self.assertEqual("FAIL", report["status"])
        self.assertTrue(report["disposition_problems"])

    def test_a_lost_event_fails(self):
        report = self.report(records=self.records[1:])
        self.assertEqual("FAIL", report["status"])

    def test_the_report_has_a_digest_and_identity(self):
        report = self.report()
        self.assertTrue(report["report_digest"].startswith("sha256:"))
        self.assertEqual("r", report["identity"]["run_id"])


class ParityNullTest(unittest.TestCase):
    NOW = datetime(2026, 6, 1, tzinfo=UTC)

    def test_a_legitimate_null_with_a_row_matches(self):
        self.assertEqual(ParityStatus.MATCH,
                         compare_feature(None, None, self.NOW, self.NOW, Ownership.HYBRID).status)

    def test_a_lost_value_is_still_missing(self):
        self.assertEqual(ParityStatus.MISSING,
                         compare_feature(2.0, None, self.NOW, self.NOW, Ownership.HYBRID).status)

    def test_a_row_without_a_timestamp_is_still_missing(self):
        self.assertEqual(ParityStatus.MISSING,
                         compare_feature(None, None, self.NOW, None, Ownership.HYBRID).status)

    def test_a_value_where_null_is_expected_is_a_mismatch(self):
        self.assertEqual(ParityStatus.MISMATCH,
                         compare_feature(None, 1.0, self.NOW, self.NOW, Ownership.HYBRID).status)


class ConsumerLoopTest(unittest.TestCase):
    def test_a_quiet_topic_ends_after_the_idle_limit(self):
        ticks = list(poll_messages(FakeConsumer(), StopSignal(), max_idle_seconds=3, clock=FakeClock()))
        self.assertEqual([None, None], ticks)

    def test_messages_reset_the_idle_timer(self):
        items = list(poll_messages(FakeConsumer([FakeMessage(), FakeMessage()]), StopSignal(),
                                   max_idle_seconds=2, clock=FakeClock()))
        self.assertEqual(2, sum(item is not None for item in items))

    def test_a_stop_request_ends_the_loop(self):
        stop = StopSignal()
        stop.request()
        self.assertEqual([], list(poll_messages(FakeConsumer([FakeMessage()]), stop)))

    def test_a_broker_error_is_raised(self):
        with self.assertRaises(RuntimeError):
            list(poll_messages(FakeConsumer([FakeMessage(error="broker down")]), StopSignal()))


class HistoryServiceTest(unittest.TestCase):
    def batcher(self, consumer, writer, size=2, interval=100.0, clock=None):
        return HistoryBatcher(consumer, writer, size, interval, clock or FakeClock())

    def test_a_full_batch_is_written_then_committed(self):
        consumer, writer = FakeConsumer(), RecordingWriter()
        batcher = self.batcher(consumer, writer)
        first, second = FakeMessage(b"a", offset=7), FakeMessage(b"b", offset=8)
        batcher.add(first)
        self.assertEqual([], writer.batches)
        batcher.add(second)
        self.assertEqual([(0, 7, [b"a", b"b"])], writer.batches)
        self.assertEqual([second], consumer.commits)

    def test_a_failed_write_commits_nothing(self):
        consumer = FakeConsumer()
        batcher = self.batcher(consumer, RecordingWriter(failures=1), size=1)
        with self.assertRaises(OSError):
            batcher.add(FakeMessage())
        self.assertEqual([], consumer.commits)

    def test_a_slow_topic_is_flushed_on_the_timer(self):
        writer = RecordingWriter()
        batcher = self.batcher(FakeConsumer(), writer, size=100, interval=1.0)
        batcher.add(FakeMessage(b"a"))
        batcher.flush_due()
        self.assertEqual(1, len(writer.batches))

    def test_partitions_are_batched_separately(self):
        writer = RecordingWriter()
        batcher = self.batcher(FakeConsumer(), writer)
        batcher.add(FakeMessage(b"a", partition=0))
        batcher.add(FakeMessage(b"b", partition=1))
        self.assertEqual([], writer.batches)
        batcher.flush_all()
        self.assertEqual(2, len(writer.batches))

    def test_invalid_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            HistoryBatcher(FakeConsumer(), RecordingWriter(), 0, 1.0)
        with self.assertRaises(ValueError):
            HistoryBatcher(FakeConsumer(), RecordingWriter(), 1, 0)

    def test_the_run_flushes_on_exit_and_reports_counts(self):
        consumer = FakeConsumer([FakeMessage(b"a")])
        writer = RecordingWriter()
        batcher = self.batcher(consumer, writer, size=100)
        stop = StopSignal()
        summary = run_history_sink(consumer, batcher, stop, max_idle_seconds=0)
        self.assertEqual({"records": 1, "objects": 1}, summary)

    def test_a_stop_request_still_flushes(self):
        consumer = FakeConsumer([FakeMessage(b"a")])
        writer = RecordingWriter()
        batcher = self.batcher(consumer, writer, size=100)
        batcher.add(consumer.poll(1))
        stop = StopSignal()
        stop.request()
        self.assertEqual(1, run_history_sink(consumer, batcher, stop)["records"])


class LocalWriterTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="history-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.writer = ImmutableLocalBatchWriter(self.root, "run-1")

    def test_a_batch_lands_in_a_run_and_partition_scoped_object(self):
        path = Path(self.writer.write(3, 9, [b"a", b"b"]))
        self.assertEqual(b"a\nb\n", path.read_bytes())
        self.assertIn("runs/run-1/partition=00003/offset=00000000000000000009-", str(path))

    def test_an_identical_replay_is_accepted(self):
        self.assertEqual(self.writer.write(0, 1, [b"a"]), self.writer.write(0, 1, [b"a"]))

    def test_the_writer_is_selected_by_scheme(self):
        self.assertIsInstance(history_writer(f"file://{self.root}", "r"), ImmutableLocalBatchWriter)
        with self.assertRaises(ValueError):
            ImmutableGcsBatchWriter("file:///x", "r", client=object())

    def test_a_run_id_is_required(self):
        with self.assertRaises(ValueError):
            ImmutableLocalBatchWriter(self.root, "")


def update_payload(**overrides) -> bytes:
    spec = FEATURE_SPECS["device_behavior"]
    update = OnlineFeatureUpdate(
        "device_behavior", spec["entity_name"], spec["entity_key"], "d1",
        datetime(2026, 6, 1, tzinfo=UTC), {feature: 1 for feature in spec["features"]}, "e1",
    )
    return json.dumps({**json.loads(update.to_json()), **overrides}).encode()


class RecordingPush:
    def __init__(self, error=None):
        self.updates = []
        self.error = error

    def write(self, update):
        if self.error:
            raise self.error
        self.updates.append(update)


class OnlineServiceTest(unittest.TestCase):
    def run_writer(self, messages, push):
        consumer = FakeConsumer(messages)
        letters = []
        summary = run_online_writer(consumer, push, letters.append, StopSignal(), max_idle_seconds=0)
        return consumer, letters, summary

    def test_a_valid_update_is_pushed_then_committed(self):
        message = FakeMessage(update_payload())
        push = RecordingPush()
        consumer, letters, summary = self.run_writer([message], push)
        self.assertEqual(1, len(push.updates))
        self.assertEqual([message], consumer.commits)
        self.assertEqual({"written": 1, "dead_lettered": 0}, summary)
        self.assertEqual([], letters)

    def test_a_store_failure_stops_without_committing(self):
        consumer = FakeConsumer([FakeMessage(update_payload())])
        with self.assertRaises(ConnectionError):
            run_online_writer(consumer, RecordingPush(ConnectionError("redis")), [].append,
                              StopSignal(), max_idle_seconds=0)
        self.assertEqual([], consumer.commits)

    def test_malformed_updates_go_to_the_dead_letter_topic(self):
        bad = (b"not json", b"[]", b'{"view_name":"x"}', update_payload(view_name="unknown"),
               update_payload(values={}))
        for payload in bad:
            message = FakeMessage(payload)
            push = RecordingPush()
            with self.subTest(payload):
                consumer, letters, summary = self.run_writer([message], push)
                self.assertEqual([], push.updates)
                self.assertEqual(1, summary["dead_lettered"])
                self.assertEqual([message], consumer.commits)
                self.assertEqual(payload.decode(),
                                 FailureEnvelope.from_json(letters[0]).original_event["raw_payload"])

    def test_a_dead_letter_failure_keeps_the_offset(self):
        consumer = FakeConsumer([FakeMessage(b"bad")])

        def unavailable(_envelope):
            raise RuntimeError("dlq down")

        with self.assertRaises(RuntimeError):
            run_online_writer(consumer, RecordingPush(), unavailable, StopSignal(), max_idle_seconds=0)
        self.assertEqual([], consumer.commits)

    def test_parse_accepts_a_complete_update(self):
        self.assertEqual("d1", parse_update(update_payload()).entity_value)

    def test_binary_garbage_is_preserved_readably(self):
        self.assertIn("raw_payload", dead_letter_envelope(b"\xff\xfe", "reason"))

    def test_the_timestamp_is_kept_with_a_sub_second_value(self):
        stamp = "2026-06-01T00:00:00.137000+00:00"
        update = parse_update(update_payload(feature_timestamp=stamp))
        self.assertEqual(137000, update.feature_timestamp.microsecond)


class ExpectedStateOrderingTest(unittest.TestCase):
    def test_the_newest_snapshot_wins_whatever_the_arrival_order(self):
        scenario = build_scenario(NAMESPACE, event_time=BASELINE.event_time)
        records, _ = parse_history(history_from(scenario))
        closing = scenario.manifest()["expected"]["closing"]
        self.assertEqual(
            expected_online_state(records, closing),
            expected_online_state(list(reversed(records)), closing),
        )

    def test_events_with_one_timestamp_do_not_see_each_other(self):
        scenario = build_scenario(NAMESPACE, event_time=BASELINE.event_time)
        first = next(event for name, event in scenario_events(scenario) if name == "normal")
        twin = replace(first, transaction_id="twin", event_id="twin-event")
        records = [HistoryRecord(first, "ON_TIME"), HistoryRecord(twin, "ON_TIME")]
        snapshot = expected_online_state(records)[("user_behavior", first.user_id)]
        self.assertEqual(0, snapshot.values["user_tx_count_1h"])
        self.assertEqual(first.event_time, snapshot.feature_timestamp)
        self.assertEqual(timedelta(0), snapshot.feature_timestamp - first.event_time)


if __name__ == "__main__":
    unittest.main()
