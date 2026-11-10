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
# The real client appends /metrics (scripts/llm_agent/g2_benchmark.py url_scraper), so a URL
# that already ends in /metrics is a 404 on GKE (EXP-V2 smoke attempt 2, 2026-11-09).
if args.epp_metrics and args.epp_metrics.rstrip("/").endswith("/metrics"):
    sys.exit("epp-metrics must be the base URL: the client appends /metrics")
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
summary = {"config": args.config, "workload_sha256": "w", "levels": {item: level for item in args.levels.split(",")}}
args.output.mkdir(parents=True, exist_ok=True)
with (args.output / "raw_requests.jsonl").open("w") as raw:
    for item in args.levels.split(","):
        for index in range(12 - int(os.environ.get("SIM_DROP_RAW", "0"))):
            raw.write(json.dumps({"level": int(item), "id": index}) + "\n")
(args.output / "summary.json").write_text(json.dumps(summary))
