"""Live check of how each agent consumes the global ModelConfig (read-only).

- Declarative agents: spec.declarative.modelConfig must name the global ModelConfig, which
  kagent resolves natively.
- BYO agents with the resolver (EWALLET_LLM_CONFIG_SOURCE=modelconfig): no Helm-rendered
  model or URL, the pod's Secret reference equals the CR's, the ServiceAccount can get only
  that ModelConfig (no list, no Secrets), and every pod logged a startup resolution of the
  current CR (same uid, model and base URL).
- BYO agents on the legacy bridge (kagent 0.10.3 has no modelConfig field for BYO): the
  running pod's env must carry exactly the ModelConfig's model and base URL, and the API key
  must come from the ModelConfig's Secret and key. BYO agents without EWALLET_LLM_* make no
  LLM call.
Reads objects with kubectl; prints env names and Secret references, never Secret values.

usage: python3 scripts/llm_agent/verify_modelconfig_bridge.py [--namespace ewallet-agents] [--out report.json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def kubectl(*args: str) -> dict:
    return json.loads(subprocess.run(["kubectl", *args, "-o", "json"], check=True, capture_output=True, text=True).stdout)


def can_i(namespace: str, sa: str, *check: str) -> bool:
    out = subprocess.run(["kubectl", "auth", "can-i", *check, "-n", namespace,
                          f"--as=system:serviceaccount:{namespace}:{sa}"], capture_output=True, text=True)
    return out.stdout.strip() == "yes"


def resolver_check(a, name: str, deploy: dict, env: dict, mc: dict, expected: dict) -> dict:
    """BYO resolver: the pod reads the CR itself; check wiring, RBAC and the startup log."""
    pod_spec = deploy["spec"]["template"]["spec"]
    sa = pod_spec.get("serviceAccountName", name)
    key = env.get("EWALLET_LLM_API_KEY", {}).get("valueFrom", {}).get("secretKeyRef", {})
    secret_ref = env.get("EWALLET_LLM_API_KEY_SECRET_REF", {}).get("value")
    resolved = []
    for pod in kubectl("get", "pods", "-n", a.namespace, "-l", f"ewallet.io/agent={name}")["items"]:
        logs = subprocess.run(["kubectl", "logs", "-n", a.namespace, pod["metadata"]["name"]],
                              capture_output=True, text=True).stdout
        lines = [json.loads(line) for line in logs.splitlines()
                 if line.startswith("{") and '"event": "modelconfig.resolved"' in line]
        resolved += [{"pod": pod["metadata"]["name"], **line} for line in lines if line.get("event") == "modelconfig.resolved"]
    checks = {
        "modelconfig_name": env.get("EWALLET_LLM_MODELCONFIG_NAME", {}).get("value") == a.modelconfig,
        "no_helm_model_or_url": "EWALLET_LLM_MODEL" not in env and "EWALLET_LLM_BASE_URL" not in env,
        "secret_ref_matches_cr": secret_ref == f"{expected['secret']['name']}/{expected['secret']['key']}"
                                 and {"name": key.get("name"), "key": key.get("key")} == expected["secret"],
        "token_volume": any(v.get("name") == "kube-api-modelconfig" for v in pod_spec.get("volumes", [])),
        "rbac_get_named": can_i(a.namespace, sa, "get", f"modelconfigs.kagent.dev/{a.modelconfig}"),
        "rbac_no_list": not can_i(a.namespace, sa, "list", "modelconfigs.kagent.dev"),
        "rbac_no_secrets": not can_i(a.namespace, sa, "get", "secrets"),
        "runtime_resolved_current_cr": bool(resolved) and all(
            r["uid"] == mc["metadata"]["uid"] and r["model"] == expected["model"]
            and r["base_url"] == expected["base_url"] for r in resolved),
    }
    return {"mechanism": "BYO resolver (reads the ModelConfig CR at startup)", "checks": checks,
            "resolved": resolved, "passed": all(checks.values()),
            "ready_replicas": deploy["status"].get("readyReplicas", 0)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--namespace", default="ewallet-agents")
    p.add_argument("--modelconfig", default="ewallet-global-llm")
    p.add_argument("--out")
    a = p.parse_args()
    mc = kubectl("get", "modelconfig", a.modelconfig, "-n", a.namespace)
    spec = mc["spec"]
    expected = {"model": spec["model"], "base_url": spec["openAI"]["baseUrl"],
                "secret": {"name": spec["apiKeySecret"], "key": spec["apiKeySecretKey"]}}
    report = {"modelconfig": {"name": a.modelconfig, **expected,
                              "accepted": any(c["type"] == "Accepted" and c["status"] == "True"
                                              for c in mc.get("status", {}).get("conditions", []))},
              "agents": {}}
    ok = report["modelconfig"]["accepted"]
    for agent in kubectl("get", "agents", "-n", a.namespace)["items"]:
        name, kind = agent["metadata"]["name"], agent["spec"]["type"]
        row: dict = {"type": kind}
        if kind == "Declarative":
            ref = agent["spec"]["declarative"].get("modelConfig")
            row.update(mechanism="native ModelConfig reference", modelConfig=ref, passed=ref == a.modelconfig)
        else:
            deploy = kubectl("get", "deployment", name, "-n", a.namespace)
            env = {e["name"]: e for c in deploy["spec"]["template"]["spec"]["containers"] for e in c.get("env", [])}
            if env.get("EWALLET_LLM_CONFIG_SOURCE", {}).get("value") == "modelconfig":
                row.update(resolver_check(a, name, deploy, env, mc, expected))
            elif "EWALLET_LLM_MODEL" not in env:
                row.update(mechanism="no LLM call (no EWALLET_LLM_* env)", passed=True)
            else:
                key = env.get("EWALLET_LLM_API_KEY", {}).get("valueFrom", {}).get("secretKeyRef", {})
                effective = {"model": env["EWALLET_LLM_MODEL"].get("value"),
                             "base_url": env.get("EWALLET_LLM_BASE_URL", {}).get("value"),
                             "secret": {"name": key.get("name"), "key": key.get("key")}}
                row.update(mechanism="configuration bridge (env rendered from the ModelConfig values)",
                           effective=effective, passed=effective == expected,
                           ready_replicas=deploy["status"].get("readyReplicas", 0))
        ok &= row["passed"]
        report["agents"][name] = row
    report["all_passed"] = ok
    text = json.dumps(report, indent=1)
    if a.out:
        open(a.out, "w").write(text + "\n")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
