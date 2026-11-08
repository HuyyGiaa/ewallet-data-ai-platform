"""Runtime check: global ModelConfig CR -> BYO resolver -> Coordinator LLM call -> agentgateway.

Runs the Coordinator's own startup and planning code outside its pod, against the live
cluster, without changing anything there:

1. ``planner_from_env()`` with ``EWALLET_LLM_CONFIG_SOURCE=modelconfig`` (exactly the env the
   chart renders) GETs ``ewallet-agents/ewallet-global-llm`` from the Kubernetes API over
   verified TLS and builds the planner from it. Locally the bearer token is the operator's
   own short-lived GKE token (in a pod it is the projected ServiceAccount token).
2. A few cases of the fixed agent suite go through the production ``plan`` path (PII
   screen, one native tool call, grounding). The approved in-cluster URL is kept; only the
   connection is redirected to a ``kubectl port-forward`` of svc/agentgateway, which routes
   the logical model to the CPU vLLM (Qwen3-0.6B).

The gateway client key comes from ``EWALLET_LLM_API_KEY`` and is never written or printed.

usage: EWALLET_LLM_API_KEY=... python3 scripts/llm_agent/modelconfig_runtime_check.py \\
         --gateway-url http://127.0.0.1:<pf> --kube-api-host <host> --token-dir <dir> --out <dir>
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ai_platform.llm_agent import nl_intake  # noqa: E402
from ops.experiments.agent_ab_eval import RedirectTransport, run_case, summarise_variant  # noqa: E402


async def run(planner, cases, run_id):
    return [await run_case(planner, case, run_id) for case in cases]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway-url", required=True, help="port-forward of svc/agentgateway (http://127.0.0.1:<port>)")
    p.add_argument("--kube-api-host", required=True)
    p.add_argument("--kube-api-port", default="443")
    p.add_argument("--token-dir", type=Path, required=True, help="holds token and ca.crt")
    p.add_argument("--suite", type=Path, default=ROOT / "configs/experiments/fixtures/agent-ab-suite.jsonl")
    p.add_argument("--cases", default="business-00,business-01,business-02,business-03,off-topic-00")
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    if not os.environ.get("EWALLET_LLM_API_KEY"):
        p.error("EWALLET_LLM_API_KEY must hold the gateway client key")
    # The chart's resolver env (templates/agents.yaml, byoResolver on).
    os.environ.update({
        "EWALLET_NL_INTAKE_ENABLED": "true", "EWALLET_LLM_CONFIG_SOURCE": "modelconfig",
        "EWALLET_LLM_MODELCONFIG_NAME": "ewallet-global-llm", "EWALLET_LLM_MODELCONFIG_NAMESPACE": "ewallet-agents",
        "EWALLET_LLM_API_KEY_SECRET_REF": "agentgateway-client-key/api-key", "EWALLET_LLM_TIMEOUT_SECONDS": "190",
        "EWALLET_KUBE_API_TOKEN_DIR": str(a.token_dir),
        "KUBERNETES_SERVICE_HOST": a.kube_api_host, "KUBERNETES_SERVICE_PORT": a.kube_api_port,
    })
    for name in ("EWALLET_LLM_BASE_URL", "EWALLET_LLM_MODEL", "EWALLET_AGENT_AB_CONFIG"):
        os.environ.pop(name, None)

    resolved = nl_intake.model_config_from_env()          # the resolution the planner below uses
    planner = nl_intake.planner_from_env()
    assert isinstance(planner, nl_intake.OpenAIToolPlanner)
    planner._transport = RedirectTransport(a.gateway_url)  # connection only; URL stays the approved one
    planner.variant = "modelconfig"

    wanted = a.cases.split(",")
    suite = {c["id"]: c for c in map(json.loads, a.suite.read_text(encoding="utf-8").splitlines()) if c}
    cases = [suite[i] for i in wanted]
    records = asyncio.run(run(planner, cases, "modelconfig-runtime"))
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "cases.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    summary = {
        "resolved_modelconfig": resolved.audit(),
        "planner": {"url": planner._url, "model": planner._model, "timeout_seconds": planner._timeout},
        "suite_sha256": hashlib.sha256(a.suite.read_bytes()).hexdigest(), "cases": wanted,
        "result": summarise_variant(records),
    }
    (a.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
