"""Sample one vLLM pod's GPU through ``nvidia-smi`` and fold the result into a run.

Runs beside a benchmark until its stop file appears. A failed read becomes a row with
``collect_error``, so a gap stays visible. ``attach`` adds the per-window figures to each
level of a client summary, which is where the comparator reads them.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Iterable, Mapping

QUERY = "memory.used,utilization.gpu"
Reader = Callable[[], str]


def kubectl_reader(context: str, namespace: str, deployment: str) -> Reader:
    """Return a reader that runs nvidia-smi in the pod."""

    def read() -> str:
        return subprocess.run(
            ["kubectl", "--context", context, "-n", namespace, "exec", f"deployment/{deployment}", "--",
             "nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True, timeout=20,
        ).stdout

    return read


def parse(output: str) -> dict[str, float]:
    """Parse one ``memory, utilization`` line; exactly one GPU is expected."""
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError(f"expected one GPU, saw {len(lines)}")
    memory, utilization = (float(value) for value in lines[0].split(","))
    return {"memory_mib": memory, "utilization_percent": utilization}


def sample(read: Reader) -> dict[str, Any]:
    """Take one timestamped sample, recording a failure instead of raising."""
    row: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat()}
    try:
        row.update(parse(read()))
    except (subprocess.SubprocessError, ValueError, OSError) as error:
        row["collect_error"] = type(error).__name__
    return row


def summarise(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return peak memory and mean utilization, counting failed samples."""
    rows = list(rows)
    good = [row for row in rows if "collect_error" not in row]
    if not good:
        return {"samples": 0, "failed_samples": len(rows), "memory_mib_peak": None, "utilization_percent_mean": None}
    return {
        "samples": len(good),
        "failed_samples": len(rows) - len(good),
        "memory_mib_peak": max(row["memory_mib"] for row in good),
        "utilization_percent_mean": round(sum(row["utilization_percent"] for row in good) / len(good), 2),
    }


def attach(summary_path: Path, samples_path: Path) -> None:
    """Add the session's GPU figures to every level of a client summary."""
    rows = [json.loads(line) for line in samples_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    gpu = summarise(rows)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    for level in summary.get("levels", {}).values():
        level["gpu"] = gpu
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    """Sample until the stop file appears, or attach samples to a summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context")
    parser.add_argument("--namespace", default="ewallet-bench")
    parser.add_argument("--deployment", default="vllm-primary")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--attach-to", type=Path)
    args = parser.parse_args()
    if args.attach_to:
        attach(args.attach_to, args.output)
        return 0
    read = kubectl_reader(args.context, args.namespace, args.deployment)
    with args.output.open("a", encoding="utf-8") as handle:
        while not args.stop_file.exists():
            handle.write(json.dumps(sample(read)) + "\n")
            handle.flush()
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
