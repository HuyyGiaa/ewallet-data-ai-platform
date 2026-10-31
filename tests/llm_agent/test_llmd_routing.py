"""llm-d inference-aware routing source contract (render only, no deploy).

Covers the llm-agent-platform side (Gateway, failover order, pool labels,
flows) and the pinned llm-d router render in infra/helm/llm-d/rendered,
validated against the InferencePool v1 contract extracted from GAIE v1.6.2.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

from ops.llm_agent.policy_lint import baseline_violations, flow_violations

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
LLMD = ROOT / "infra/helm/llm-d"
HELM = shutil.which("helm")
PINS = dict(line.split("=", 1) for line in (LLMD / "versions.env").read_text().splitlines()
            if line and not line.startswith("#"))


def render(*overrides):
    with tempfile.TemporaryDirectory() as folder:
        args = [HELM, "template", "llm-agent", str(CHART), "-f", str(CHART / "values-gcp.yaml")]
        for index, override in enumerate(overrides):
            path = Path(folder) / f"override-{index}.yaml"
            path.write_text(yaml.safe_dump(override))
            args += ["-f", str(path)]
        return subprocess.run(args, capture_output=True, text=True, check=False)


def documents(proc):
    assert proc.returncode == 0, proc.stderr
    return [item for item in yaml.safe_load_all(proc.stdout) if item]


def find(items, kind, name):
    return next(i for i in items if i["kind"] == kind and i["metadata"]["name"] == name)


def gateway_config(items):
    return yaml.safe_load(find(items, "ConfigMap", "agentgateway-llm-config")["data"]["config.yaml"])


ON = {"llmd": {"enabled": True}}


@unittest.skipUnless(HELM, "helm binary unavailable")
class LlmdChartTests(unittest.TestCase):
    def test_disabled_by_default_keeps_direct_routing(self):
        items = documents(render())
        self.assertFalse([i for i in items if i["kind"] == "Gateway" and i["metadata"]["name"] == "llm-inference-gateway"])
        targets = gateway_config(items)["llm"]["virtualModels"][0]["routing"]["failover"]["targets"]
        self.assertEqual([t["model"] for t in targets], ["ewallet-fraud-llm-primary", "ewallet-fraud-llm-fallback"])
        pod = find(items, "Deployment", "vllm-primary")["spec"]["template"]["metadata"]["labels"]
        self.assertNotIn("llm-d.ai/inference-pool", pod)

    def test_enabled_routes_through_the_pool_and_keeps_direct_failover(self):
        items = documents(render(ON))
        gateway = find(items, "Gateway", "llm-inference-gateway")
        self.assertEqual(gateway["spec"]["gatewayClassName"], "agentgateway")
        self.assertEqual(gateway["spec"]["listeners"][0]["allowedRoutes"], {"namespaces": {"from": "Same"}})
        llm = gateway_config(items)["llm"]
        targets = llm["virtualModels"][0]["routing"]["failover"]["targets"]
        self.assertEqual([(t["model"], t["priority"]) for t in targets],
                         [("ewallet-fraud-llm-routed", 0), ("ewallet-fraud-llm-primary", 1),
                          ("ewallet-fraud-llm-fallback", 2)])
        models = {m["name"]: m for m in llm["models"]}
        routed = models["ewallet-fraud-llm-routed"]
        self.assertEqual(routed["params"]["baseUrl"],
                         "http://llm-inference-gateway.ewallet-agents.svc.cluster.local:80/v1")
        self.assertEqual(routed["responseHeaders"]["set"]["x-ewallet-backend"], "llm-d")
        self.assertIn("eviction", routed["health"])
        # Same served model on every path: the pool never changes model identity.
        self.assertEqual({m["params"]["model"] for m in models.values()}, {"ewallet-fraud-llm-v1"})
        self.assertIn("vllm-primary.ewallet-agents", models["ewallet-fraud-llm-primary"]["params"]["baseUrl"])

    def test_only_primary_pods_join_the_pool(self):
        items = documents(render(ON))
        for name, expected in (("vllm-primary", "ewallet-fraud-llm"), ("vllm-fallback", None)):
            with self.subTest(name):
                labels = find(items, "Deployment", name)["spec"]["template"]["metadata"]["labels"]
                self.assertEqual(labels.get("llm-d.ai/inference-pool"), expected)

    def test_flows_stay_least_privilege(self):
        items = documents(render(ON))
        self.assertEqual(flow_violations(items, namespace="ewallet-agents"), [])
        self.assertEqual(baseline_violations(items), [])
        policy = find(items, "AuthorizationPolicy", "allow-llmd-epp-to-vllm-metrics")
        self.assertEqual(policy["spec"]["rules"][0]["from"][0]["source"]["principals"],
                         ["cluster.local/ns/ewallet-agents/sa/ewallet-fraud-llm-epp"])
        flows = find(items, "ConfigMap", "llm-agent-network-flows")["data"]
        unresolved = {f["name"] for f in json.loads(flows["unresolved.json"])}
        self.assertTrue({"agentgateway-to-llm-inference-gateway", "llmd-epp-to-kube-apiserver"} <= unresolved)
        self.assertIn("llmd-epp-to-vllm-metrics", {f["name"] for f in json.loads(flows["enforced.json"])})

    def test_unsupported_configurations_are_rejected(self):
        cases = {
            "other gateway class": {"llmd": {"enabled": True, "gatewayClassName": "istio"}},
            "empty pool": {"llmd": {"enabled": True}, "vllm": {"primary": {"replicas": 0}}},
            "no agentgateway": {"llmd": {"enabled": True}, "agentgateway": {"enabled": False}},
            "bad pool name": {"llmd": {"enabled": True, "poolName": "Pool_1"}},
        }
        for label, override in cases.items():
            with self.subTest(label):
                self.assertNotEqual(render(override).returncode, 0)


class LlmdRouterRenderTests(unittest.TestCase):
    """The pinned llm-d router render matches the chart side and GAIE v1."""

    @classmethod
    def setUpClass(cls):
        cls.items = [i for i in yaml.safe_load_all(
            (LLMD / f"rendered/router-{PINS['LLMD_ROUTER_VERSION']}.yaml").read_text()) if i]
        cls.contract = json.loads((LLMD / f"rendered/inferencepool-{PINS['GAIE_VERSION']}-contract.json").read_text())
        cls.values = yaml.safe_load((CHART / "values.yaml").read_text())

    def test_pins_are_digest_locked_and_consistent(self):
        for key in ("AGENTGATEWAY_CRDS_DIGEST", "AGENTGATEWAY_CHART_DIGEST", "LLMD_ROUTER_CHART_DIGEST"):
            self.assertRegex(PINS[key], r"^sha256:[0-9a-f]{64}$")
        for key in ("GAIE_MANIFEST_SHA256", "GATEWAY_API_DOC_SHA256"):
            self.assertRegex(PINS[key], r"^[0-9a-f]{64}$")
        # Controller and the standalone LLM agentgateway are the same release.
        self.assertIn(f":{PINS['AGENTGATEWAY_VERSION']}@", self.values["agentgateway"]["image"])
        self.assertTrue(PINS["LLMD_EPP_IMAGE"].endswith(":" + PINS["LLMD_ROUTER_VERSION"]))
        self.assertIn(f"/{PINS['GAIE_VERSION']}/", PINS["GAIE_MANIFEST_URL"])

    def test_inferencepool_matches_the_gaie_v1_contract(self):
        pool = find(self.items, "InferencePool", self.values["llmd"]["poolName"])
        self.assertEqual(pool["apiVersion"], f"{self.contract['group']}/{self.contract['version']}")
        spec = pool["spec"]
        self.assertTrue(set(self.contract["spec_required"]) <= set(spec))
        self.assertTrue(set(spec) <= set(self.contract["spec_fields"]))
        self.assertTrue(set(spec["endpointPickerRef"]) <= set(self.contract["endpointPickerRef_fields"]))
        self.assertIn(spec["endpointPickerRef"]["failureMode"], self.contract["failureMode_enum"])
        self.assertLessEqual(len(spec["targetPorts"]), self.contract["targetPorts_maxItems"])

    def test_pool_selects_the_chart_primary_pods_on_the_vllm_port(self):
        pool = find(self.items, "InferencePool", self.values["llmd"]["poolName"])["spec"]
        self.assertEqual(pool["selector"]["matchLabels"], {"llm-d.ai/inference-pool": self.values["llmd"]["poolName"]})
        self.assertEqual(pool["targetPorts"], [{"number": 8000}])
        epp = find(self.items, "Service", f"{PINS['LLMD_RELEASE']}-epp")
        self.assertIn(pool["endpointPickerRef"]["port"]["number"], [p["port"] for p in epp["spec"]["ports"]])
        self.assertEqual(pool["endpointPickerRef"]["name"], epp["metadata"]["name"])

    def test_epp_failure_is_explicit_and_observable(self):
        pool = find(self.items, "InferencePool", self.values["llmd"]["poolName"])["spec"]
        self.assertEqual(pool["endpointPickerRef"]["failureMode"], "FailClose")
        container = find(self.items, "Deployment", f"{PINS['LLMD_RELEASE']}-epp")["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], PINS["LLMD_EPP_IMAGE"])
        for probe in ("readinessProbe", "livenessProbe"):
            self.assertEqual(container[probe]["grpc"]["port"], 9003)
        self.assertIn("--tracing=true", container["args"])

    def test_route_binds_the_chart_gateway_only(self):
        route = find(self.items, "HTTPRoute", PINS["LLMD_RELEASE"])["spec"]
        self.assertEqual([p["name"] for p in route["parentRefs"]], [self.values["llmd"]["gatewayName"]])
        self.assertEqual([(b["group"], b["kind"]) for b in route["rules"][0]["backendRefs"]],
                         [("inference.networking.k8s.io", "InferencePool")])

    def test_cpu_scope_no_accelerator_or_disaggregation(self):
        text = (LLMD / f"rendered/router-{PINS['LLMD_ROUTER_VERSION']}.yaml").read_text()
        for marker in ("nvidia.com/gpu", "nixl", "prefill", "decode", "rdma", "kv-events"):
            self.assertNotIn(marker, text.lower())
        epp = find(self.items, "Deployment", f"{PINS['LLMD_RELEASE']}-epp")
        resources = epp["spec"]["template"]["spec"]["containers"][0]["resources"]
        self.assertEqual(resources["requests"]["cpu"], "100m")
        self.assertEqual(epp["spec"]["replicas"], 1)
        rbac = [i for i in self.items if i["kind"] in ("Role", "ClusterRole")]
        self.assertTrue(rbac and all(i["kind"] == "Role" for i in rbac))
        for role in rbac:
            for rule in role["rules"]:
                self.assertTrue(set(rule["verbs"]) <= {"get", "list", "watch", "create", "update", "patch"}, rule)

    def test_epp_identity_matches_the_declared_flow_workload(self):
        epp = find(self.items, "Deployment", f"{PINS['LLMD_RELEASE']}-epp")["spec"]["template"]
        workload = self.values["networkFlows"]["workloads"]["llmd-epp"]
        self.assertEqual(epp["spec"]["serviceAccountName"], workload["serviceAccount"])
        for key, value in workload["podLabels"].items():
            self.assertEqual(epp["metadata"]["labels"][key], value)


if __name__ == "__main__":
    unittest.main()
