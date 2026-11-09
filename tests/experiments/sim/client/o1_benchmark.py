#!/usr/bin/env python3
"""Stand-in client: writes a summary in the real shape, fails when told to."""
import argparse, json, os, sys
from pathlib import Path

parser = argparse.ArgumentParser()
for name in ("config", "gateway", "metrics", "levels", "timeout", "allocated-cpu", "resources", "stop-file", "context",
             "namespace", "pods", "epp-metrics"):
    parser.add_argument("--" + name)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
state = Path(os.environ["SIM_STATE"])
assert os.environ["EWALLET_GATEWAY_KEY"], "client key missing"
(state / "clients.log").open("a").write(f"{args.config} {args.output.parent.parent.parent.name}/{args.output.name}\n")
count = sum(1 for _ in (state / "clients.log").open())
if str(count) == os.environ.get("SIM_FAIL_RUN"):
    sys.exit(7)
if str(count) == os.environ.get("SIM_STOP_AT"):
    Path(args.stop_file).touch()
fast = args.config in {"c1-thread-bind", "o2"}
level = {"status": "MEASURED", "requests": 12, "succeeded": 12, "e2e_ms": {"p50": 50, "p95": 70 if fast else 100},
         "ttft_ms": {"p50": 5, "p95": 7}, "tpot_ms": {"p50": 2}, "output_tokens_per_second": 20 if fast else 10,
         "requests_per_second": 1, "resources": None}
summary = {"config": args.config, "workload_sha256": "w", "levels": {item: dict(level) for item in args.levels.split(",")}}
first = args.levels.split(",")[0]
# SIM_PARTIAL_RUN: that client call loses one request (exit 1, like the real clients);
# SIM_BLOCKED_RUN: its first level is answered by the gateway's rate limit.
partial = str(count) == os.environ.get("SIM_PARTIAL_RUN")
blocked = str(count) == os.environ.get("SIM_BLOCKED_RUN")
if partial:
    summary["levels"][first].update(succeeded=11, error_rate=1 / 12)
if blocked:
    summary["levels"][first] = {"status": "BLOCKED_GATEWAY_RATE_LIMIT", "rate_limited": 1}
args.output.mkdir(parents=True, exist_ok=True)
with (args.output / "raw_requests.jsonl").open("w") as raw:
    for item in args.levels.split(","):
        for index in range(12 - int(os.environ.get("SIM_DROP_RAW", "0"))):
            raw.write(json.dumps({"level": int(item), "id": index}) + "\n")
(args.output / "summary.json").write_text(json.dumps(summary))
if partial or blocked:
    sys.exit(1)
