"""Build a baseline-vs-optimized Markdown report from two evidence run dirs.

Only values present in the run evidence are printed; missing ones show as
``n/a``. No verdict beyond what the numbers show is generated.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: Path):
    return json.loads(path.read_text()) if path.is_file() else {}


def _last_snapshot(run: Path) -> dict:
    snapshots = sorted((run / "metrics").glob("snapshot_*.json"))
    return _load(snapshots[-1]) if snapshots else {}


def _vertex_max(snapshot: dict, metric: str):
    vertices = snapshot.get("vertices")
    if not isinstance(vertices, list):
        return None
    values = [v["metrics"].get(metric, {}).get("max") for v in vertices]
    values = [x for x in values if isinstance(x, (int, float))]
    return max(values) if values else None


def run_row(run: Path) -> dict:
    meta, manifest = _load(run / "run.json"), _load(run / "input" / "input_manifest.json")
    outputs, snap = _load(run / "outputs_summary.json"), _last_snapshot(run)
    publish = manifest.get("publish", {})
    checkpoints = snap.get("checkpoints", {}) if isinstance(snap.get("checkpoints"), dict) else {}
    summary = checkpoints.get("summary") or {}
    counts = checkpoints.get("counts") or {}
    latency = outputs.get("processing_latency_ms", {})
    return {
        "profile": meta.get("profile"),
        "git_sha": meta.get("git_sha"),
        "config_sha256": meta.get("config_sha256"),
        "job_id": snap.get("job_id") or meta.get("job_id"),
        "input_sha256": manifest.get("payload_sha256"),
        "target_rate_eps": publish.get("target_rate_eps"),
        "achieved_send_rate_eps": publish.get("achieved_send_rate_eps"),
        "input_records": outputs.get("record_counts", {}).get("raw"),
        "accepted_records": outputs.get("record_counts", {}).get("accepted"),
        "online_records": outputs.get("record_counts", {}).get("online"),
        "processing_throughput_eps": outputs.get("processing_throughput_eps"),
        "latency_p50_ms": latency.get("p50"),
        "latency_p95_ms": latency.get("p95"),
        "latency_p99_ms": latency.get("p99"),
        "consumer_lag": (snap.get("consumer_lag") or {}).get("total_lag"),
        "max_busy_ms_per_s": _vertex_max(snap, "busyTimeMsPerSecond"),
        "max_backpressured_ms_per_s": _vertex_max(snap, "backPressuredTimeMsPerSecond"),
        "checkpoints_completed": counts.get("completed"),
        "checkpoints_failed": counts.get("failed"),
        "checkpoint_duration_p50_ms": (summary.get("end_to_end_duration") or {}).get("p50"),
        "checkpoint_duration_max_ms": (summary.get("end_to_end_duration") or {}).get("max"),
        "checkpointed_size_max_bytes": (summary.get("checkpointed_size") or {}).get("max"),
        "state_size_max_bytes": (summary.get("state_size") or {}).get("max"),
        "functional_checks_passed": outputs.get("functional_checks", {}).get("all_passed"),
        "parity": outputs.get("parity", {}),
    }


def report(baseline: Path, optimized: Path) -> str:
    rows = {"baseline": run_row(baseline), "optimized": run_row(optimized)}
    same_input = rows["baseline"]["input_sha256"] == rows["optimized"]["input_sha256"] and rows["baseline"]["input_sha256"]
    parity = {
        key: rows["baseline"]["parity"].get(key) == rows["optimized"]["parity"].get(key)
        for key in ("accepted_sha256", "online_sha256", "late_sha256", "dlq_count")
    }
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
