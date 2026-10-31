"""Policy-only MCP Gateway and A2A gateway source contract (render only).

The rendered agentgateway configs are additionally parsed by the pinned
binary (scripts/llm_agent/validate_gateway_configs.sh) and exercised against a
header-recording stub (scripts/llm_agent/gateway_smoke/run.sh); the last test
checks the committed smoke evidence still shows the fail-closed outcome.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from ops.llm_agent.policy_lint import baseline_violations, flow_violations

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
EVIDENCE = ROOT / "docs/evidence/llm_agent/source_review/gateways"
HELM = shutil.which("helm")
MCP = {"mcpGateway": {"enabled": True}}
CALLERS = {"mcpGateway": {"enabled": True, "callerServiceAccounts": {
    "drift-specialist": "worker-drift", "feature-rag-specialist": "worker-feature-rag"}}}
STRIPPED = ["authorization", "x-ewallet-actor", "x-ewallet-caller"]


def render(*overrides, check=True):
    with tempfile.TemporaryDirectory() as folder:
        args = [HELM, "template", "llm-agent", str(CHART), "-f", str(CHART / "values-gcp.yaml")]
        for index, override in enumerate(overrides):
            path = Path(folder) / f"override-{index}.yaml"
            path.write_text(yaml.safe_dump(override))
            args += ["-f", str(path)]
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if not check:
        return proc
    assert proc.returncode == 0, proc.stderr
    return [item for item in yaml.safe_load_all(proc.stdout) if item]


def config(items, name):
    configmap = next(i for i in items if i["kind"] == "ConfigMap" and i["metadata"]["name"] == name)
    return yaml.safe_load(configmap["data"]["config.yaml"])


def routes(items, name):
    return {route["name"]: route for route in config(items, name)["routes"]}


@unittest.skipUnless(HELM, "helm binary unavailable")
class McpGatewayTests(unittest.TestCase):
    def test_disabled_by_default(self):
        names = {i["metadata"]["name"] for i in render() if i["kind"] == "Deployment"}
        self.assertFalse(names & {"mcp-gateway", "a2a-gateway"})

    def test_without_read_back_identities_every_tool_is_denied(self):
        for route in routes(render(MCP), "mcp-gateway-config").values():
            self.assertEqual(route["policies"]["mcpAuthorization"]["rules"], ["false"])

    def test_rules_bind_mesh_identity_to_the_agent_allow_list(self):
        rendered = routes(render(CALLERS), "mcp-gateway-config")
        feature = rendered["feature-rag-mcp"]["policies"]["mcpAuthorization"]["rules"]
        drift = rendered["drift-mcp"]["policies"]["mcpAuthorization"]["rules"]
        self.assertEqual(feature, [
            'has(source.identity) && source.identity.namespace == "ewallet-agents" && '
            'source.identity.serviceAccount == "worker-drift" && mcp.tool.name in ["feature_lookup"]',
            'has(source.identity) && source.identity.namespace == "ewallet-agents" && '
            'source.identity.serviceAccount == "worker-feature-rag" && '
            'mcp.tool.name in ["feature_lookup","fraud_scoring","knowledge_search"]',
        ])
        # Cross-agent: only drift-specialist may call drift_detection.
        self.assertEqual(len(drift), 1)
        self.assertIn('"worker-drift"', drift[0])
        self.assertIn('["drift_detection"]', drift[0])

    def test_identity_headers_and_credentials_are_never_forwarded(self):
        for route in routes(render(CALLERS), "mcp-gateway-config").values():
            policies = route["policies"]
            self.assertEqual(policies["requestHeaderModifier"]["remove"], STRIPPED)
            self.assertEqual(policies["transformations"]["request"]["set"],
                             {"x-ewallet-caller": "source.identity.serviceAccount"})
            self.assertEqual(policies["localRateLimit"][0]["type"], "requests")
            self.assertIn("requestTimeout", policies["timeout"])
        for route in routes(render({"a2aGateway": {"enabled": True}}), "a2a-gateway-config").values():
            self.assertEqual(route["policies"]["requestHeaderModifier"]["remove"], STRIPPED)

    def test_each_route_has_one_target_on_the_matching_server(self):
        for name, route in routes(render(MCP), "mcp-gateway-config").items():
            targets = route["backends"][0]["mcp"]["targets"]
            self.assertEqual(len(targets), 1)  # no tool-name prefixing (prefixMode conditional)
            self.assertNotIn("_", targets[0]["name"])
            self.assertEqual(targets[0]["mcp"]["host"], f"http://{name}.ewallet-agents.svc.cluster.local:8080/mcp")

    def test_gateway_bypass_is_denied_by_flows(self):
        items = render(CALLERS, {"mcpServices": {"enabled": False}})
        self.assertEqual(flow_violations(items, namespace="ewallet-agents"), [])
        for server in ("feature-rag-mcp", "drift-mcp"):
            policy = next(i for i in items if i["kind"] == "AuthorizationPolicy"
                          and i["metadata"]["name"] == f"allow-mcp-gateway-to-{server}")
            principals = [p for rule in policy["spec"]["rules"] for s in rule["from"] for p in s["source"]["principals"]]
            self.assertEqual(principals, ["cluster.local/ns/ewallet-agents/sa/mcp-gateway"])
        allowed_into_servers = {i["metadata"]["name"] for i in items if i["kind"] == "AuthorizationPolicy"
                                and i["spec"].get("selector", {}).get("matchLabels", {}).get("app.kubernetes.io/name")
                                in ("feature-rag-mcp", "drift-mcp")}
        self.assertEqual(allowed_into_servers, {"allow-mcp-gateway-to-feature-rag-mcp", "allow-mcp-gateway-to-drift-mcp"})

    def test_invalid_caller_identity_is_rejected(self):
        bad = {"mcpGateway": {"enabled": True, "callerServiceAccounts": {"drift-specialist": "Bad SA!"}}}
        self.assertNotEqual(render(bad, check=False).returncode, 0)

    def test_gateway_workloads_are_hardened(self):
        items = render(CALLERS, {"a2aGateway": {"enabled": True}})
        self.assertEqual(baseline_violations(items), [])
        for name in ("mcp-gateway", "a2a-gateway"):
            pod = next(i for i in items if i["kind"] == "Deployment" and i["metadata"]["name"] == name)["spec"]["template"]
            self.assertEqual(pod["spec"]["serviceAccountName"], name)
            self.assertFalse(pod["spec"]["automountServiceAccountToken"])
            self.assertTrue(pod["spec"]["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
            self.assertRegex(pod["metadata"]["annotations"]["checksum/config"], r"^[0-9a-f]{64}$")


@unittest.skipUnless(HELM, "helm binary unavailable")
class A2aGatewayTests(unittest.TestCase):
    def test_a2a_route_is_native_kagent_a2a_for_this_namespace_only(self):
        items = render({"a2aGateway": {"enabled": True, "callerServiceAccounts": {"fraud-coordinator": "worker-coordinator"}}})
        route = routes(items, "a2a-gateway-config")["kagent-a2a"]
        self.assertEqual(route["matches"], [{"path": {"pathPrefix": "/api/a2a/ewallet-agents/"}}])
        self.assertEqual(route["policies"]["a2a"], {})
        self.assertEqual(route["backends"], [{"host": "kagent-controller.kagent.svc.cluster.local:8083"}])
        self.assertEqual(route["policies"]["authorization"]["rules"], [
            'has(source.identity) && source.identity.namespace == "ewallet-agents" && '
            'source.identity.serviceAccount == "worker-coordinator"'])
        default = routes(render({"a2aGateway": {"enabled": True}}), "a2a-gateway-config")["kagent-a2a"]
        self.assertEqual(default["policies"]["authorization"]["rules"], ["false"])


class SmokeEvidenceTests(unittest.TestCase):
    """The committed local smoke must keep showing the fail-closed outcome."""

    def test_no_identity_means_no_tool_execution_and_no_header_leak(self):
        smoke = json.loads((EVIDENCE / "mcp_gateway_policy_smoke.json").read_text())
        for variant in ("deny-all", "callers"):
            seen = smoke[variant]["backend_saw"]
            self.assertFalse([s for s in seen if s["method"] == "tools/call"], variant)
            for path, result in smoke[variant]["client"].items():
                self.assertEqual(result["tools_list"]["tools"], [], (variant, path))
                self.assertTrue(all(c["status"] >= 400 for c in result["tools_call"].values()))
        for variant, data in smoke.items():
            if variant.startswith("_"):
                continue
            for seen in data["backend_saw"]:
                self.assertFalse(seen["authorization_present"], variant)
                self.assertIsNone(seen["x-ewallet-actor"], variant)
                self.assertIsNone(seen["x-ewallet-caller"], variant)
        allowed = smoke["test-only-identity-free"]["client"]["/feature-rag/mcp"]
        self.assertEqual(allowed["tools_list"]["tools"], ["feature_lookup"])
        self.assertEqual(allowed["tools_call"]["fraud_scoring"]["status"], 400)

    def test_every_rendered_config_passed_the_pinned_binary(self):
        lines = (EVIDENCE / "agentgateway_validate_only.txt").read_text().splitlines()
        self.assertIn("overall: PASS", lines)
        self.assertTrue(any(line.startswith("negative-control: Error") for line in lines))


if __name__ == "__main__":
    unittest.main()
