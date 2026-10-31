"""Snapshot Flink REST and consumer-group metrics for one run.

Container CPU/RAM is sampled on the host by run_benchmark.sh (docker stats),
because the bench container has no Docker socket.

Every value is copied from a real endpoint or command; anything unavailable is
recorded as an error string, never estimated.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from data_platform.streaming.contracts.topics import consumer_group, topic_name


VERTEX_METRICS = (
    "numRecordsIn",
    "numRecordsOut",
    "numRecordsInPerSecond",
    "numRecordsOutPerSecond",
    "busyTimeMsPerSecond",
    "backPressuredTimeMsPerSecond",
    "idleTimeMsPerSecond",
)


def get_json(base: str, path: str):
    with urllib.request.urlopen(f"{base.rstrip('/')}{path}", timeout=15) as response:
        return json.loads(response.read())


def _safe(fn, *args):
    try:
        return fn(*args)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
        return {"error": f"{type(error).__name__}: {error}"}


def running_job_id(base: str) -> str:
    jobs = [job for job in get_json(base, "/jobs/overview")["jobs"] if job["state"] == "RUNNING"]
    if len(jobs) != 1:
        raise RuntimeError(f"expected exactly one RUNNING job, found {[j['jid'] for j in jobs]}")
    return jobs[0]["jid"]


def vertex_metrics(base: str, job_id: str) -> list[dict]:
    job = get_json(base, f"/jobs/{job_id}")
    result = []
    for vertex in job["vertices"]:
        query = ",".join(VERTEX_METRICS)
        aggregated = get_json(
            base, f"/jobs/{job_id}/vertices/{vertex['id']}/subtasks/metrics?get={query}&agg=sum,max,avg"
        )
        result.append(
            {
                "id": vertex["id"],
                "name": vertex["name"],
                "parallelism": vertex["parallelism"],
                "metrics": {item["id"]: {k: v for k, v in item.items() if k != "id"} for item in aggregated},
                "rest_counters": vertex.get("metrics", {}),
            }
        )
    return result


def checkpoints(base: str, job_id: str) -> dict:
    data = get_json(base, f"/jobs/{job_id}/checkpoints")
    history = [
        {
            key: item.get(key)
            for key in ("id", "status", "is_savepoint", "end_to_end_duration", "state_size",
                        "checkpointed_size", "trigger_timestamp", "num_acknowledged_subtasks")
        }
        for item in data.get("history", [])
    ]
    return {"counts": data.get("counts"), "summary": data.get("summary"), "history": history}


def consumer_lag(bootstrap_servers: str, namespace: str) -> dict:
    """Committed-offset lag. Flink's KafkaSource commits only on checkpoint completion."""
    from confluent_kafka import Consumer, TopicPartition

    group, topics = consumer_group("flink", namespace), [topic_name("raw", namespace)]
    consumer = Consumer({"bootstrap.servers": bootstrap_servers, "group.id": group, "enable.auto.commit": False})
    try:
        partitions = []
        for topic in topics:
            meta = consumer.list_topics(topic, timeout=10).topics[topic]
            partitions += [TopicPartition(topic, p) for p in meta.partitions]
        committed = consumer.committed(partitions, timeout=10)
        lag, rows = 0, []
        for tp in committed:
            _low, high = consumer.get_watermark_offsets(tp, timeout=10)
            offset = tp.offset if tp.offset >= 0 else None
            part_lag = high - offset if offset is not None else high
            lag += part_lag
            rows.append({"topic": tp.topic, "partition": tp.partition, "committed": offset, "high": high, "lag": part_lag})
        return {"group": group, "total_lag": lag, "partitions": rows,
                "note": "committed offsets advance only when a checkpoint completes"}
    finally:
        consumer.close()


def snapshot(base: str, bootstrap_servers: str, namespace: str, job_id: str | None) -> dict:
    job_id = job_id or running_job_id(base)
    return {
        "taken_at": datetime.now().astimezone().isoformat(),
        "job_id": job_id,
        "job": _safe(get_json, base, f"/jobs/{job_id}"),
        "vertices": _safe(vertex_metrics, base, job_id),
        "checkpoints": _safe(checkpoints, base, job_id),
        "job_config": _safe(get_json, base, f"/jobs/{job_id}/config"),
        "taskmanagers": _safe(get_json, base, "/taskmanagers"),
        "consumer_lag": _safe(consumer_lag, bootstrap_servers, namespace),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--flink-url", default="http://jobmanager:8081")
    result.add_argument("--bootstrap-servers", default="redpanda:29092")
    result.add_argument("--namespace", required=True)
    result.add_argument("--job-id")
    result.add_argument("--run-dir", type=Path, required=True)
    result.add_argument("--interval-seconds", type=float, default=10.0)
    result.add_argument("--samples", type=int, default=1)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if not 1 <= args.samples <= 720:
        raise SystemExit("--samples must be in [1, 720]")
    out = args.run_dir / "metrics"
    out.mkdir(parents=True, exist_ok=True)
    for index in range(args.samples):
        data = snapshot(args.flink_url, args.bootstrap_servers, args.namespace, args.job_id)
        args.job_id = data["job_id"]
        (out / f"snapshot_{index:04d}.json").write_text(json.dumps(data, indent=2, sort_keys=True))
        if index + 1 < args.samples:
            time.sleep(args.interval_seconds)
    print(json.dumps({"job_id": args.job_id, "snapshots": args.samples, "dir": str(out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
