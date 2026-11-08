"""Owner evidence checkpoint text for ``vllm_bench.sh`` (EXP_CAPTURE_MODE=interactive).

Prints what the owner needs to capture after one run: the arm's real configuration (read
back from the pods), the client's own summary, the evidence paths, the Grafana link for
exactly this run's window and the commands to show configuration and inventory. It only
reads files the run already wrote and never prints credentials.

usage: python -m ops.experiments.checkpoint --title "EXP-V1 / BASELINE run 01" --run-dir <raw/run-01> ...
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ops.experiments.bench_dashboard import UID

RULE = "=" * 64


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)


def _read(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _fmt(value: Any, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def summary_lines(summary: dict[str, Any] | None) -> list[str]:
    """One line per concurrency level from the client's ``summary.json``."""

    if not summary:
        return ["  (no summary.json: the client did not finish)"]
    lines = [f"  workload {summary.get('workload_version')} sha256 {str(summary.get('workload_sha256'))[:12]}..."]
    for level, data in sorted(summary.get("levels", {}).items(), key=lambda item: int(item[0])):
        if data.get("status") != "MEASURED":
            lines.append(f"  c={level:<2} {data.get('status')}")
            continue
        lines.append(f"  c={level:<2} out tok/s {_fmt(data.get('output_tokens_per_second'))}"
                     f" | TTFT p50 {_fmt((data.get('ttft_ms') or {}).get('p50'), 0)} ms"
                     f" | E2E p95 {_fmt((data.get('e2e_ms') or {}).get('p95'), 0)} ms"
                     f" | errors {_fmt(data.get('error_rate'), 3)} | n={data.get('requests')}")
    return lines


def banner(args: argparse.Namespace) -> str:
    """Build the checkpoint text."""

    controls = _read(args.controls) or {}
    readback = _read(args.readback) or {}
    policy = _read(args.policy)
    grafana = (f"{args.grafana}/d/{UID}/?orgId=1&from={_ms(args.window_start)}&to={_ms(args.window_end) + 60000}"
               f"&var-run_id={args.run_id}") if args.window_start and args.window_end else f"{args.grafana}/d/{UID}/"
    ctx = f"kubectl --context {args.kube_context} -n ewallet-bench"
    lines = [RULE, f"{args.title} — OWNER EVIDENCE CHECKPOINT", RULE, "",
             f"Experiment:     {args.experiment}",
             "Model:          Qwen/Qwen3-0.6B @ " + str(controls.get("model_revision", "n/a")),
             f"Arm:            {args.arm} ({args.label})",
             f"Thread binding: {controls.get('thread_bind', 'n/a')}   replicas: {controls.get('replicas', 'n/a')}",
             f"Route:          {args.route}",
             f"Image digest:   {controls.get('image_digest', 'n/a')}",
             f"Nodes:          {controls.get('node_names', 'n/a')}",
             "Namespace:      ewallet-bench (temporary benchmark stack, not production)",
             f"Run ID:         {args.run_id}",
             f"Window (UTC):   {args.window_start or 'n/a'} -> {args.window_end or 'n/a'}", "",
             "Benchmark completed (client summary, authoritative):", *summary_lines(_read(args.summary)), "",
             f"Raw output:     {args.run_dir}",
             f"Prometheus:     {args.prometheus}   (readback: "
             f"{(readback.get('summary') or {}).get('with_data', 'n/a')}/"
             f"{(readback.get('summary') or {}).get('queries', 'n/a')} queries with data)",
             f"Grafana:        {grafana}"]
    if policy:
        lines.append(f"Gateway policy: {policy.get('verdict')}  ({args.policy})")
    lines += ["", "Capture now (save PNGs under the path below):",
              f"  screenshots -> {args.screenshots}/{args.run_id}-<name>.png",
              "  1 deployment configuration:",
              f"      {ctx} get deploy vllm-primary -o jsonpath='{{.spec.template.spec.containers[0].env}}'; echo",
              f"      {ctx} get configmap agentgateway-llm-config -o jsonpath='{{.data.config\\.yaml}}' | grep -E 'baseUrl|x-ewallet-backend|maxTokens'",
              f"  2 benchmark terminal result: this screen, or  cat {args.run_dir}/summary.json",
              "  3 Grafana panels for the same window: the link above",
              f"  4 pods and nodes:   {ctx} get pods -o wide; kubectl --context {args.kube_context} get nodes -l ewallet.io/bench=true -o wide"]
    if args.route == "llm-d EPP":
        lines.append(f"  5 EPP routing log:  {ctx} logs deploy/llmd-router-epp -c epp --tail=40 | grep -i 'endpoint'")
    lines += ["", "No credentials are shown here. Type CONTINUE to proceed or ABORT to stop and clean up.",
              f"This checkpoint waits at most {args.wait_minutes} min, then stops the session and cleans up.", RULE]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Print the checkpoint banner."""

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("title", "experiment", "arm", "label", "route", "run-id", "kube-context", "grafana", "prometheus",
                 "window-start", "window-end", "wait-minutes"):
        parser.add_argument(f"--{name}", default="")
    for name in ("run-dir", "summary", "controls", "readback", "policy", "screenshots"):
        parser.add_argument(f"--{name}", type=Path)
    args = parser.parse_args(argv)
    print(banner(args), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
