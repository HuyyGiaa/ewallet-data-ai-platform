"""G2 local preflight on rendered manifests: nothing reaches the cluster unless every check passes.

Inputs: the llm-agent renders for B2 and O2 and the llm-d router render
(g2_experiment.sh step 0). Checks:
- the vLLM Deployments are byte-identical between B2 and O2 (routing is the only variable);
- C1 on both primaries' replicas: 2 replicas, OpenMP bind 0-1, 2 threads, prefix caching on,
  KV 2 GiB, 2/3 CPU and 6/10 GiB, max-model-len 4096, image by digest;
- B2 routes agentgateway -> vllm-primary; O2 -> the router Service and port, with the
  direct primary and fallback still behind it; same token/request limits in both;
- router: EPP selector = the primary pod labels, port 8000, fail-closed proxy, images by
  digest, no cluster-scoped object, nodes restricted to the G2 label;
- NetworkPolicy/Istio: O2 opens exactly agentgateway->router:8081, router->vllm:8000 and
  router->API server, nothing else changes;
- token budget: two full passes plus the gates fit the session token rate limit (x1.5).

usage: python3 scripts/llm_agent/g2_preflight.py --b2 b2.yaml --o2 o2.yaml --router router.yaml \
    --levels 1,2,4,8 --output preflight.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import g2_benchmark  # noqa: E402

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
GATE_TOKENS = 8000      # smoke stream/non-stream x2 gates, failover probes, warm-up probes
BUDGET_MARGIN = 1.5


def docs(text: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(text) if d]


def by_kind(items: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {d["metadata"]["name"]: d for d in items if d["kind"] == kind}


def gateway_llm(items: list[dict[str, Any]]) -> dict[str, Any]:
    return yaml.safe_load(by_kind(items, "ConfigMap")["agentgateway-llm-config"]["data"]["config.yaml"])["llm"]


def check(b2: list[dict[str, Any]], o2: list[dict[str, Any]], router: list[dict[str, Any]],
          levels: list[int]) -> dict[str, Any]:
    problems: list[str] = []
    db, do = by_kind(b2, "Deployment"), by_kind(o2, "Deployment")
    for name in ("vllm-primary", "vllm-fallback"):
        if db.get(name) != do.get(name):
            problems.append(f"{name} differs between B2 and O2")
    primary = db["vllm-primary"]
    c = primary["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in c["env"]}
    expected_env = {"VLLM_CPU_OMP_THREADS_BIND": "0-1", "OMP_NUM_THREADS": "2", "VLLM_CPU_KVCACHE_SPACE": "2"}
    for key, value in expected_env.items():
        if env.get(key) != value:
            problems.append(f"C1: {key}={env.get(key)}, expected {value}")
    if primary["spec"]["replicas"] != 2:
        problems.append("primary replicas must be 2")
    if "--enable-prefix-caching" not in c["args"] or c["args"][c["args"].index("--max-model-len") + 1] != "4096":
        problems.append("C1: prefix caching on and max-model-len 4096 expected")
    if c["resources"] != {"requests": {"cpu": "2", "memory": "6Gi"}, "limits": {"cpu": "3", "memory": "10Gi"}}:
        problems.append(f"C1 resources {c['resources']}")
    if primary["spec"]["template"]["spec"].get("nodeSelector") != {"ewallet.io/g2-llm": "true"}:
        problems.append("vLLM pods must be restricted to the G2 nodes")

    lb, lo = gateway_llm(b2), gateway_llm(o2)
    if lb["policies"] != lo["policies"]:
        problems.append("gateway policies (rate limits, timeouts) differ between B2 and O2")
    targets_b = [t["model"] for t in lb["virtualModels"][0]["routing"]["failover"]["targets"]]
    targets_o = [t["model"] for t in lo["virtualModels"][0]["routing"]["failover"]["targets"]]
    if targets_b != ["ewallet-fraud-llm-primary", "ewallet-fraud-llm-fallback"]:
        problems.append(f"B2 failover targets {targets_b}")
    if targets_o != ["ewallet-fraud-llm-routed", "ewallet-fraud-llm-primary", "ewallet-fraud-llm-fallback"]:
        problems.append(f"O2 failover targets {targets_o}")
    routed = next(m for m in lo["models"] if m["name"] == "ewallet-fraud-llm-routed")["params"]["baseUrl"]

    rdep = next(iter(by_kind(router, "Deployment").values()))
    rsvc = {n: s for n, s in by_kind(router, "Service").items() if any(p["port"] == 8081 for p in s["spec"]["ports"])}
    if not rsvc or not any(routed.startswith(f"http://{n}.ewallet-agents.svc.cluster.local:8081/") for n in rsvc):
        problems.append(f"O2 routed baseUrl {routed} is not the router Service on 8081")
    pod_labels = primary["spec"]["template"]["metadata"]["labels"]
    epp = next(x for x in rdep["spec"]["template"]["spec"]["containers"] if x["name"] == "epp")
    selector = epp["args"][epp["args"].index("--endpoint-selector") + 1]
    wanted = dict(kv.split("=", 1) for kv in selector.split(","))
    if any(pod_labels.get(k) != v for k, v in wanted.items()) or wanted.get("app.kubernetes.io/instance") != "primary":
        problems.append(f"EPP selector {selector} does not select exactly the primary pods")
    if epp["args"][epp["args"].index("--endpoint-target-ports") + 1] != "8000":
        problems.append("EPP target port must be 8000")
    for x in rdep["spec"]["template"]["spec"]["containers"]:
        if not DIGEST.search(x["image"]):
            problems.append(f"router image {x['image']} not pinned by digest")
    envoy_cm = next((d for d in router if d["kind"] == "ConfigMap" and "envoy.yaml" in d.get("data", {})), None)
    if not envoy_cm or "failure_mode_allow: false" not in envoy_cm["data"]["envoy.yaml"]:
        problems.append("router proxy must fail closed (failure_mode_allow: false)")
    if any(d["kind"].startswith("Cluster") or d["kind"] == "CustomResourceDefinition" for d in router):
        problems.append("router render has cluster-scoped objects")
    affinity = json.dumps(rdep["spec"]["template"]["spec"].get("affinity", {}))
    if "ewallet.io/g2-llm" not in affinity:
        problems.append("router pod is not restricted to the G2 nodes")

    def policies(items):
        return {d["metadata"]["name"] for d in items if d["kind"] in ("NetworkPolicy", "AuthorizationPolicy")}
    added = policies(o2) - policies(b2)
    expected_added = {"allow-agentgateway-to-llmd-router", "allow-llmd-router-to-vllm",
                      "allow-agentgateway-to-llmd-router-ingress", "allow-agentgateway-to-llmd-router-egress",
                      "allow-llmd-router-to-vllm-ingress", "allow-llmd-router-to-vllm-egress",
                      "allow-llmd-router-to-kube-apiserver", "allow-ambient-probes-llmd-router"}
    if added != expected_added or policies(b2) - policies(o2):
        problems.append(f"O2 policy delta {sorted(added)} (removed {sorted(policies(b2) - policies(o2))})")

    token_rate = next(r for r in lb["policies"]["localRateLimit"] if r["type"] == "tokens")
    need = g2_benchmark.budget(g2_benchmark.o1.build_workload(), levels)
    required = int((2 * need["pass_total"] + GATE_TOKENS) * BUDGET_MARGIN)
    if token_rate["maxTokens"] < required:
        problems.append(f"token budget {token_rate['maxTokens']} < required {required}")
    return {"ok": not problems, "problems": problems, "token_budget": {"rate": token_rate, "per_pass": need,
                                                                       "required_with_margin": required},
            "endpoint_selector": selector, "routed_base_url": routed, "o2_policies_added": sorted(added)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("b2", "o2", "router", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--levels", default="1,2,4,8")
    args = parser.parse_args(argv)
    result = check(docs(args.b2.read_text()), docs(args.o2.read_text()), docs(args.router.read_text()),
                   [int(x) for x in args.levels.split(",")])
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ("ok", "problems", "token_budget")}, indent=1))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
