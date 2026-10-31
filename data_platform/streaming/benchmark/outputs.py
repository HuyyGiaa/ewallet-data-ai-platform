"""Read one namespace's topics back and derive counts, latency and parity digests.

Processing latency here is broker LogAppendTime(accepted) - LogAppendTime(first
raw arrival) for the same transaction_id: it covers Flink processing plus sink
delivery. It is NOT event lateness (arrival_time - event_time), which the
generator injects on purpose and is reported separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

from data_platform.streaming.benchmark.replay import BENCH_TOPICS
from data_platform.streaming.contracts.topics import topic_name


def read_topic(bootstrap_servers: str, topic: str, timeout: float = 30.0) -> list[tuple[str, str, int]]:
    """Return (key, value, broker_timestamp_ms) up to the high watermarks seen at call time."""
    from confluent_kafka import Consumer, TopicPartition

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap_servers,
            "group.id": f"ewallet-bench-reader-{topic}",
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    try:
        metadata = consumer.list_topics(topic, timeout=10).topics[topic]
        if metadata.error is not None:
            raise RuntimeError(f"{topic}: {metadata.error}")
        targets = {}
        for partition in metadata.partitions:
            low, high = consumer.get_watermark_offsets(TopicPartition(topic, partition), timeout=10)
            if high > low:
                targets[partition] = high
        consumer.assign([TopicPartition(topic, p, 0) for p in targets])
        records: list[tuple[str, str, int]] = []
        done: set[int] = set()
        idle_polls = 0
        while len(done) < len(targets):
            message = consumer.poll(1.0)
            if message is None:
                idle_polls += 1
                if idle_polls > timeout:
                    raise TimeoutError(f"{topic}: stalled before high watermarks {targets}")
                continue
            idle_polls = 0
            if message.error():
                raise RuntimeError(str(message.error()))
            key = message.key().decode() if message.key() else ""
            records.append((key, message.value().decode(), message.timestamp()[1]))
            if message.offset() + 1 >= targets[message.partition()]:
                done.add(message.partition())
        return records
    finally:
        consumer.close()


def high_watermarks(bootstrap_servers: str, topics: list[str]) -> int:
    from confluent_kafka import Consumer, TopicPartition

    consumer = Consumer({"bootstrap.servers": bootstrap_servers, "group.id": "ewallet-bench-probe"})
    try:
        total = 0
        for topic in topics:
            meta = consumer.list_topics(topic, timeout=10).topics[topic]
            for partition in meta.partitions:
                total += consumer.get_watermark_offsets(TopicPartition(topic, partition), timeout=10)[1]
        return total
    finally:
        consumer.close()


def wait_for_quiescence(bootstrap_servers: str, namespace: str, stable_seconds: int, timeout_seconds: int) -> dict:
    """Wait until output topic offsets stop growing; report rather than guess on timeout."""
    import time

    topics = [topic_name(name, namespace) for name in ("accepted", "late", "dlq", "online", "retry")]
    started = time.monotonic()
    last, last_change = -1, started
    while True:
        current = high_watermarks(bootstrap_servers, topics)
        now = time.monotonic()
        if current != last:
            last, last_change = current, now
        if now - last_change >= stable_seconds:
            return {"quiesced": True, "output_offsets": current, "waited_seconds": round(now - started, 1)}
        if now - started >= timeout_seconds:
            return {"quiesced": False, "output_offsets": current, "waited_seconds": round(now - started, 1)}
        time.sleep(2)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(q / 100 * len(ordered)) - 1)
    return ordered[rank]


def digest(lines) -> str:
    result = hashlib.sha256()
    for line in sorted(lines):
        result.update(line.encode() + b"\n")
    return result.hexdigest()


def summarize(topics: dict[str, list[tuple[str, str, int]]], manifest: dict) -> dict:
    raw, accepted = topics["raw"], topics["accepted"]
    first_raw = {}
    for key, _value, ts in raw:
        first_raw[key] = min(ts, first_raw.get(key, ts))
    latencies = [ts - first_raw[key] for key, _value, ts in accepted if key in first_raw]
    dispositions = Counter(json.loads(value).get("disposition", "") for _k, value, _t in accepted)
    accepted_keys = [key for key, _v, _t in accepted]
    online_lines = [value for _k, value, _t in topics["online"]]
    window_s = None
    if raw and accepted:
        window_s = (max(ts for *_x, ts in accepted) - min(ts for *_x, ts in raw)) / 1000
    summary = {
        "record_counts": {name: len(records) for name, records in topics.items()},
        "accepted_unique_transactions": len(set(accepted_keys)),
        "accepted_duplicate_keys": len(accepted_keys) - len(set(accepted_keys)),
        "accepted_dispositions": dict(sorted(dispositions.items())),
        "processing_latency_ms": {
            "definition": "LogAppendTime(accepted) - first LogAppendTime(raw) per transaction_id",
            "samples": len(latencies),
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "max": max(latencies) if latencies else None,
        },
        "processing_throughput_eps": round(len(accepted) / window_s, 2) if window_s else None,
        "parity": {
            "accepted_sha256": digest(f"{k}|{json.loads(v).get('disposition')}" for k, v, _t in accepted),
            "online_sha256": digest(online_lines),
            "late_sha256": digest(k for k, _v, _t in topics["late"]),
            "dlq_count": len(topics["dlq"]),
        },
    }
    summary["functional_checks"] = functional_checks(summary, manifest)
    return summary


def functional_checks(summary: dict, manifest: dict) -> dict:
    """Invariants that hold for any correct run of the bounded replay."""
    counts = summary["record_counts"]
    checks = {
        "raw_count_matches_input": counts["raw"] == manifest["emitted_records"],
        "dedup_exact_once_per_transaction": summary["accepted_unique_transactions"]
        == manifest["unique_transactions"]
        and summary["accepted_duplicate_keys"] == 0,
        "invalid_records_reach_dlq": counts["dlq"] == manifest["invalid_records"],
        "too_late_not_more_than_injected": counts["late"] <= manifest["late_arrival_transactions"],
        "online_features_emitted": counts["online"] > 0,
        "no_retry_records": counts["retry"] == 0,
    }
    return {"all_passed": all(checks.values()), **checks}


def feature_oracle(topics: dict[str, list[tuple[str, str, int]]]) -> dict:
    """Recompute every emitted online update with the pure-Python reference state.

    Uses Flink's own dispositions as input, so it verifies window/feature math,
    not lateness classification. Only timestamps whose timers fired are compared.
    """
    from data_platform.streaming.contracts.events import TransactionEvent
    from data_platform.streaming.flink.feature_state import HistoricalFeatureState
    from data_platform.streaming.config.settings import BASELINE

    eligible = {}
    for _key, value, _ts in topics["accepted"]:
        record = json.loads(value)
        if record.get("disposition") != "TOO_LATE":
            eligible[record["transaction_id"]] = record
    raw_events = {}
    for _key, value, _ts in topics["raw"]:
        try:
            event = TransactionEvent.from_json(value)
        except ValueError:
            continue
        raw_events.setdefault(event.transaction_id, event)
    events = sorted((raw_events[tx] for tx in eligible), key=lambda e: (e.event_time, e.transaction_id))
    state = HistoricalFeatureState(history_ttl=BASELINE.history_ttl)
    expected = {}
    index = 0
    while index < len(events):
        group = [e for e in events[index:] if e.event_time == events[index].event_time]
        snapshots = state.snapshot_group(group)
        for event in group:
            for view, snapshot in snapshots[event.transaction_id].items():
                expected[(view, snapshot.entity_value, event.event_id)] = snapshot.values
        index += len(group)
    mismatches = []
    compared = 0
    for _key, value, _ts in topics["online"]:
        item = json.loads(value)
        key = (item["view_name"], item["entity_value"], item["source_event_id"])
        compared += 1
        want = expected.get(key)
        got = item["values"]
        if want is None or set(want) != set(got) or any(
            not _close(want[name], got[name]) for name in want
        ):
            if len(mismatches) < 20:
                mismatches.append({"key": list(key), "expected": want, "actual": got})
    return {"compared": compared, "mismatch_count_sampled": len(mismatches), "mismatches": mismatches,
            "passed": compared > 0 and not mismatches}


def _close(a, b) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-6)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--namespace", required=True)
    result.add_argument("--bootstrap-servers", default="redpanda:29092")
    result.add_argument("--run-dir", type=Path, required=True)
    result.add_argument("--oracle", action="store_true", help="functional mode: recompute features")
    result.add_argument("--wait-only", action="store_true", help="only wait for output quiescence")
    result.add_argument("--wait-stable-seconds", type=int, default=30)
    result.add_argument("--wait-timeout-seconds", type=int, default=900)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.wait_only:
        state = wait_for_quiescence(args.bootstrap_servers, args.namespace, args.wait_stable_seconds, args.wait_timeout_seconds)
        (args.run_dir / "quiescence.json").write_text(json.dumps(state, indent=2))
        print(json.dumps(state))
        return 0 if state["quiesced"] else 3
    manifest = json.loads((args.run_dir / "input" / "input_manifest.json").read_text())
    topics = {name: read_topic(args.bootstrap_servers, topic_name(name, args.namespace)) for name in BENCH_TOPICS}
    summary = summarize(topics, manifest)
    if args.oracle:
        summary["feature_oracle"] = feature_oracle(topics)
    summary["collected_at"] = datetime.now().astimezone().isoformat()
    (args.run_dir / "outputs_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps({"record_counts": summary["record_counts"], "functional_checks": summary["functional_checks"]}))
    return 0 if summary["functional_checks"]["all_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
