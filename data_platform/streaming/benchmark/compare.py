"""Build a baseline-vs-optimized Markdown report from two evidence run dirs.

Only values present in the run evidence are printed; missing ones show as
``n/a``. No verdict beyond what the numbers show is generated.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from data_platform.streaming.benchmark.outputs import acceptance_passed


def _load(path: Path):
    return json.loads(path.read_text()) if path.is_file() else {}


def _snapshots(run: Path) -> list[dict]:
    loaded = [(path, _load(path)) for path in (run / "metrics").glob("snapshot_*.json")]

    def chronological(item):
        path, snapshot = item
        try:
            return (1, datetime.fromisoformat(snapshot["taken_at"]).timestamp(), path.name)
        except (KeyError, TypeError, ValueError):
            return (0, path.name)

    return [snapshot for _path, snapshot in sorted(loaded, key=chronological)]


def _last_completed_checkpoint_snapshot(run: Path) -> dict:
    completed = [
        snapshot
        for snapshot in _snapshots(run)
        if (((snapshot.get("checkpoints") or {}).get("counts") or {}).get("completed") or 0) > 0
    ]
    return completed[-1] if completed else {}


def _vertex_max(snapshot: dict, metric: str):
    vertices = snapshot.get("vertices")
    if not isinstance(vertices, list):
        return None
    values = [v["metrics"].get(metric, {}).get("max") for v in vertices]
    values = [x for x in values if isinstance(x, (int, float))]
    return max(values) if values else None


def _vertex_named_max(snapshot: dict, metric: str, token: str):
    vertices = snapshot.get("vertices")
    if not isinstance(vertices, list):
        return None
    values = [
        vertex["metrics"].get(metric, {}).get("max")
        for vertex in vertices
        if token.lower() in vertex.get("name", "").lower()
    ]
    values = [value for value in values if isinstance(value, (int, float))]
    return max(values) if values else None


def _maximum(values):
    measured = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return max(measured) if measured else None


def run_row(run: Path) -> dict:
    meta, manifest = _load(run / "run.json"), _load(run / "input" / "input_manifest.json")
    outputs, snapshots = _load(run / "outputs_summary.json"), _snapshots(run)
    snap = snapshots[-1] if snapshots else {}
    checkpoint_snap = _last_completed_checkpoint_snapshot(run)
    publish = manifest.get("publish", {})
    phase_snapshots = snapshots
    phases = manifest.get("phases") or {}
    if publish.get("started_at") and phases.get("warmup_seconds") is not None:
        steady_start = datetime.fromisoformat(publish["started_at"]) + timedelta(
            seconds=float(phases["warmup_seconds"])
        )
        steady_end = datetime.fromisoformat(publish["finished_at"])
        selected = [
            item for item in snapshots
            if item.get("taken_at")
            and steady_start <= datetime.fromisoformat(item["taken_at"]) <= steady_end
        ]
        if selected:
            phase_snapshots = selected
    checkpoints = (
        checkpoint_snap.get("checkpoints", {})
        if isinstance(checkpoint_snap.get("checkpoints"), dict)
        else {}
    )
    summary = checkpoints.get("summary") or {}
    counts = checkpoints.get("counts") or {}
    latency = outputs.get("processing_latency_ms", {})
    feature_latency = outputs.get("feature_processing_latency_ms", {})
    return {
        "profile": meta.get("profile"),
        "git_sha": meta.get("git_sha"),
        "config_sha256": meta.get("config_sha256"),
        "job_id": snap.get("job_id") or meta.get("job_id"),
        "input_sha256": manifest.get("payload_sha256"),
        "transport_input_sha256": manifest.get("transport_payload_sha256"),
        "target_rate_eps": publish.get("target_rate_eps"),
        "achieved_send_rate_eps": publish.get("achieved_send_rate_eps"),
        "input_records": outputs.get("record_counts", {}).get("raw"),
        "accepted_records": outputs.get("record_counts", {}).get("accepted"),
        "online_records": outputs.get("record_counts", {}).get("online"),
        "processing_throughput_eps": outputs.get("processing_throughput_eps"),
        "latency_p50_ms": latency.get("p50"),
        "latency_p95_ms": latency.get("p95"),
        "latency_p99_ms": latency.get("p99"),
        "feature_latency_p50_ms": feature_latency.get("p50"),
        "feature_latency_p95_ms": feature_latency.get("p95"),
        "feature_latency_p99_ms": feature_latency.get("p99"),
        "warmup_seconds": phases.get("warmup_seconds"),
        "steady_state_seconds": phases.get("steady_state_seconds"),
        "telemetry_samples": len(snapshots),
        "steady_telemetry_samples": len(phase_snapshots),
        "max_consumer_lag": _maximum((item.get("consumer_lag") or {}).get("total_lag") for item in phase_snapshots),
        "max_source_rate_eps": _maximum(
            _vertex_named_max(item, "numRecordsOutPerSecond", "source") for item in phase_snapshots
        ),
        "max_sink_rate_eps": _maximum(
            _vertex_named_max(item, "numRecordsInPerSecond", "sink") for item in phase_snapshots
        ),
        "max_source_sink_rate_eps": _maximum(
            _vertex_max(item, metric)
            for item in phase_snapshots
            for metric in ("numRecordsInPerSecond", "numRecordsOutPerSecond")
        ),
        "max_busy_ms_per_s": _maximum(_vertex_max(item, "busyTimeMsPerSecond") for item in phase_snapshots),
        "max_backpressured_ms_per_s": _maximum(
            _vertex_max(item, "backPressuredTimeMsPerSecond") for item in phase_snapshots
        ),
        "checkpoints_completed": counts.get("completed"),
        "checkpoints_failed": counts.get("failed"),
        "checkpoint_duration_p50_ms": (summary.get("end_to_end_duration") or {}).get("p50"),
        "checkpoint_duration_max_ms": (summary.get("end_to_end_duration") or {}).get("max"),
        "checkpointed_size_max_bytes": (summary.get("checkpointed_size") or {}).get("max"),
        "state_size_max_bytes": (summary.get("state_size") or {}).get("max"),
        "functional_checks_passed": outputs.get("functional_checks", {}).get("all_passed"),
        "parity": outputs.get("parity", {}),
    }


def validate_run(run: Path) -> None:
    """Only runs the driver finalized as complete count as performance evidence."""
    meta = _load(run / "run.json")
    outputs = _load(run / "outputs_summary.json")
    problems = []
    if meta.get("status") != "complete":
        problems.append(f"status={meta.get('status')!r} failures={meta.get('failures')}")
    if meta.get("git_dirty") is True:
        problems.append("run source provenance is dirty")
    mode = meta.get("mode")
    if mode not in {"functional", "performance"}:
        problems.append(f"unsupported or missing mode={mode!r}")
    if not outputs.get("functional_checks", {}).get("all_passed"):
        problems.append("functional checks missing or failed")
    elif mode == "functional" and not acceptance_passed(outputs, require_oracle=True):
        problems.append("feature oracle missing, incomplete or failed")
    if not _last_completed_checkpoint_snapshot(run):
        problems.append("no completed checkpoint in metrics")
    if mode == "performance" and len(_snapshots(run)) < 2:
        problems.append("performance run lacks in-flight telemetry samples")
    if problems:
        raise ValueError(f"{run}: invalid run: {'; '.join(problems)}")


def report(baseline: Path, optimized: Path, *, require_valid: bool = True) -> str:
    if require_valid:
        for run in (baseline, optimized):
            validate_run(run)
    rows = {"baseline": run_row(baseline), "optimized": run_row(optimized)}
    same_input = rows["baseline"]["input_sha256"] == rows["optimized"]["input_sha256"] and rows["baseline"]["input_sha256"]
    if require_valid and not same_input:
        raise ValueError("logical input payload SHA-256 is missing or mismatched")
    parity = {
        key: rows["baseline"]["parity"].get(key) == rows["optimized"]["parity"].get(key)
        for key in ("accepted_sha256", "online_sha256", "late_sha256", "dlq_count")
    }
    if require_valid and not all(parity.values()):
        failed = ", ".join(key for key, passed in parity.items() if not passed)
        raise ValueError(f"semantic output parity mismatch: {failed}")
    lines = [
        "# Flink baseline vs optimized — measured evidence",
        "",
        f"- Baseline run: `{baseline}`",
        f"- Optimized run: `{optimized}`",
        f"- Identical logical input (payload SHA-256): **{bool(same_input)}**",
        f"- Output parity: {', '.join(f'{k}={v}' for k, v in parity.items())}",
        "",
        "| Metric | Baseline | Optimized |",
        "|---|---|---|",
    ]
    for key in rows["baseline"]:
        if key == "parity":
            continue
        values = [rows[name][key] for name in ("baseline", "optimized")]
        lines.append(f"| {key} | {' | '.join('n/a' if v is None else str(v) for v in values)} |")
    lines += [
        "",
        "Latency is broker LogAppendTime(accepted) − LogAppendTime(raw) per transaction,",
        "not event lateness. Consumer lag uses committed offsets, which Flink advances",
        "only on checkpoint completion. Status: PERFORMANCE evidence only for this run;",
        "repeat runs are needed before drawing a performance conclusion.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    args = argparse.ArgumentParser()
    args.add_argument("--baseline", type=Path, required=True)
    args.add_argument("--optimized", type=Path, required=True)
    args.add_argument("--out", type=Path, required=True)
    parsed = args.parse_args(argv)
    text = report(parsed.baseline, parsed.optimized)
    parsed.out.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
