#!/usr/bin/env bash
# Fake g1_operator.sh for the offline G2 runner simulation: writes the evidence
# files g2_experiment.sh reads (smoke backend from the routing in force).
set -euo pipefail
export G2_SIM_DIR=$(cd "$(dirname "$0")" && pwd)
stage=$1 evidence=$2
mkdir -p "$evidence"
case ",${G2_SIM_FAIL:-}," in *",stage-$stage,"*) echo "simulated $stage failure" >&2; exit 1 ;; esac
routing=$(python3 -c 'import json,os; print(json.load(open(os.environ["G2_SIM_STATE"]))["routing"] or "b2")')
backend=primary; [ "$routing" = o2 ] && backend=llm-d
case "$stage" in
  smoke-gateway) echo "{\"first_turn\": {\"backend\": \"$backend\"}}" > "$evidence/smoke-gateway-non-stream.json" ;;
  netpol-verify) printf 'unlabelled-to-vllm\tdeny\tdeny\n' > "$evidence/netpol-verify.tsv" ;;
  failover)  # like g1_operator: primaries scaled to 0, then restored as new pods
    python3 - <<'PY'
import sys
sys.path.insert(0, __import__("os").environ["G2_SIM_DIR"])
import simstate
with simstate.locked() as s:
    for pod in [p for p, v in s["pods"].items() if v.get("deployment") == "vllm-primary"]:
        old = s["pods"].pop(pod)
        new = simstate.new_pod(s, "vllm-primary", old["node"], old["labels"], old["image"])
        s["pods"][new]["deployment"] = "vllm-primary"
PY
    ;;
esac
echo "$stage ok" >> "$evidence/operator.log"
