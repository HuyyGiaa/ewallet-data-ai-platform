"""Render the isolated ModelConfig-resolver canary for the kagent-pilot namespace.

Input: `helm template` of the llm-agent chart with modelConfig.byoResolver on and
namespace.name=kagent-pilot (the production values otherwise). Output: the chart's own
Role, RoleBinding and egress NetworkPolicy, a copy of the live global ModelConfig spec, and three
kagent BYO Agents whose deployment block (resolver env, projected token volume) is taken
verbatim from the rendered Coordinator Agent, with only these changes:

* name, ServiceAccount and the `ewallet.io/agent` label per canary agent;
* command `python /canary/canary_resolver.py`, plus the code ConfigMap mounted at /canary;
* env reduced to the LLM/ModelConfig variables (the canary has no Redis or MCP Secrets);
* small resources, one replica.

Every object carries `ewallet.io/canary: modelconfig-resolver` so teardown is one selector.

usage: python3 render_canary.py <helm-template.yaml> <out.yaml> --denied-egress <ip:port> \
         --live-modelconfig <modelconfig.json>
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

import yaml

NAMESPACE = "kagent-pilot"
LABEL = {"ewallet.io/canary": "modelconfig-resolver"}
# canary Agent -> (ServiceAccount, ewallet.io/agent label)
AGENTS = {
    "mc-canary-coordinator": ("fraud-coordinator", "fraud-coordinator"),
    "mc-canary-feature-rag": ("feature-rag-specialist", "feature-rag-specialist"),
    # Inside the egress policy but with no RoleBinding: must fail with modelconfig_forbidden.
    "mc-canary-unbound": ("mc-canary-unbound", "fraud-coordinator"),
}
KEEP_ENV = ("EWALLET_NL_INTAKE_ENABLED", "EWALLET_LLM_", "EWALLET_KUBE_API_TOKEN_DIR")


def labelled(doc: dict) -> dict:
    meta = doc.setdefault("metadata", {})
    labels = meta.setdefault("labels", {})
    labels.pop("app.kubernetes.io/managed-by", None)
    labels.update(LABEL)
    meta.pop("annotations", None)
    return doc


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("rendered", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--denied-egress", required=True)
    p.add_argument("--live-modelconfig", type=Path, required=True,
                   help="kubectl -n ewallet-agents get modelconfig ewallet-global-llm -o json")
    a = p.parse_args()
    docs = [d for d in yaml.safe_load_all(a.rendered.read_text()) if d]
    pick = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    out = [labelled(copy.deepcopy(pick[key])) for key in (
        ("Role", "ewallet-global-llm-reader"), ("RoleBinding", "ewallet-global-llm-reader"),
        ("NetworkPolicy", "allow-byo-modelconfig-to-kube-apiserver"), ("ModelConfig", "ewallet-global-llm"))]
    for doc in out:
        assert doc["metadata"]["namespace"] == NAMESPACE, doc["metadata"]
    # The chart derives the base URL from namespace.name; the canary copies the live
    # production spec instead, so it resolves exactly what the production agents would.
    live = yaml.safe_load(a.live_modelconfig.read_text())
    assert live["metadata"]["namespace"] == "ewallet-agents" and live["kind"] == "ModelConfig"
    out[3]["spec"] = copy.deepcopy(live["spec"])

    source = pick[("Agent", "fraud-coordinator")]
    assert source["metadata"]["namespace"] == NAMESPACE
    base = source["spec"]["byo"]["deployment"]
    assert any(v["name"] == "kube-api-modelconfig" for v in base["volumes"]), "render with byoResolver on"
    for sa in sorted({sa for sa, _ in AGENTS.values()}):
        out.append(labelled({"apiVersion": "v1", "kind": "ServiceAccount", "automountServiceAccountToken": False,
                             "metadata": {"name": sa, "namespace": NAMESPACE}}))
    for name, (sa, agent_label) in AGENTS.items():
        dep = copy.deepcopy(base)
        dep.update(
            serviceAccountName=sa, replicas=1, cmd="python", args=["/canary/canary_resolver.py"],
            labels={"ewallet.io/agent": agent_label, **LABEL},
            annotations={"ewallet.io/canary-of": "fraud-coordinator"},
            resources={"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"cpu": "200m", "memory": "192Mi"}},
            env=[e for e in dep["env"] if e["name"].startswith(KEEP_ENV)]
                + [{"name": "CANARY_DENIED_EGRESS", "value": a.denied_egress}],
        )
        dep.pop("nodeSelector", None)
        dep["volumes"].append({"name": "canary-code", "configMap": {"name": "mc-canary-code", "defaultMode": 0o444}})
        dep["volumeMounts"].append({"name": "canary-code", "mountPath": "/canary", "readOnly": True})
        out.append(labelled({"apiVersion": "kagent.dev/v1alpha2", "kind": "Agent",
                             "metadata": {"name": name, "namespace": NAMESPACE},
                             "spec": {"type": "BYO", "description": "ModelConfig resolver canary (temporary)",
                                      "byo": {"deployment": dep}}}))
    a.out.write_text(yaml.safe_dump_all(out, sort_keys=False))
    print(f"{len(out)} objects -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
