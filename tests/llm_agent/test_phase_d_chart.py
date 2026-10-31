"""Helm source contract for the Phase D MCP tool servers (render only, no deploy)."""

from __future__ import annotations

import ast
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
HELM = shutil.which("helm")
DIGEST = "sha256:" + "c" * 64
ENABLED = {"mcpServices": {
    "enabled": True,
    "image": f"example.invalid/ewallet/mcp-tools@{DIGEST}",
    "identity": {"gitSha": "a" * 40, "imageDigest": DIGEST, "configRevision": "cfg-1",
                 "modelRevision": "fraud-xgb/7", "featureRevision": "feat-1", "corpusRevision": "corpus-1"},
    "services": {"feature-rag-mcp": {"env": {
        "EWALLET_FEAST_REPO_PATH": "/app/feature_repo", "EWALLET_KSERVE_BASE_URL": "http://kserve.invalid",
        "EWALLET_MODEL_NAME": "fraud-xgb", "EWALLET_MODEL_VERSION": "7"}}},
}}


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        result[key] = _merge(result.get(key, {}), value) if isinstance(value, dict) else value
    return result


@unittest.skipUnless(HELM, "helm binary unavailable")
class McpChartTests(unittest.TestCase):
    def render(self, override=None, check=True):
        with tempfile.TemporaryDirectory() as folder:
            args = [HELM, "template", "llm-agent", str(CHART), "-f", str(CHART / "values-gcp.yaml")]
            if override is not None:
                path = Path(folder) / "override.yaml"
                path.write_text(yaml.safe_dump(override))
                args += ["-f", str(path)]
            proc = subprocess.run(args, capture_output=True, text=True, check=False)
        if not check:
            return proc
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [item for item in yaml.safe_load_all(proc.stdout) if item]

    def test_disabled_by_default(self):
        names = {(item["kind"], item["metadata"]["name"]) for item in self.render()}
        self.assertNotIn(("Deployment", "feature-rag-mcp"), names)
        self.assertNotIn(("Deployment", "drift-mcp"), names)

    def test_enabled_render_is_least_privilege_and_rolling(self):
        items = self.render(ENABLED)
        deployments = {i["metadata"]["name"]: i for i in items if i["kind"] == "Deployment"}
        factories = {node.name for node in ast.parse((ROOT / "ai_platform/llm_agent/tools/service.py").read_text()).body
                     if isinstance(node, ast.FunctionDef)}
        for name in ("feature-rag-mcp", "drift-mcp"):
            with self.subTest(name):
                spec = deployments[name]["spec"]
                pod = spec["template"]["spec"]
                container = pod["containers"][0]
                self.assertEqual(spec["replicas"], 2)
                self.assertEqual(spec["strategy"]["rollingUpdate"], {"maxUnavailable": 0, "maxSurge": 1})
                self.assertEqual(pod["serviceAccountName"], name)
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertTrue(pod["securityContext"]["runAsNonRoot"])
                self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
                self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
                self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
                self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/health/ready")
                self.assertEqual(container["livenessProbe"]["httpGet"]["path"], "/health/live")
                self.assertEqual(container["startupProbe"]["httpGet"]["path"], "/health/live")
                self.assertIn("limits", container["resources"])
                self.assertTrue(container["image"].endswith(DIGEST))
                env = {item["name"]: item["value"] for item in container["env"]}
                self.assertEqual(env["EWALLET_IMAGE_DIGEST"], DIGEST)
                factory = container["command"][container["command"].index("--factory") + 1]
                self.assertEqual(factory.split(":")[0], "ai_platform.llm_agent.tools.service")
                self.assertIn(factory.split(":")[1], factories)
        pdbs = {i["metadata"]["name"] for i in items if i["kind"] == "PodDisruptionBudget"}
        self.assertEqual(pdbs, {"feature-rag-mcp", "drift-mcp"})
        self.assertEqual(baseline_violations(items), [])
        self.assertEqual(flow_violations(items, namespace="ewallet-agents"), [])

    def test_mcp_servers_stay_behind_default_deny(self):
        items = self.render(ENABLED)
        for item in items:
            if item["kind"] in ("NetworkPolicy", "AuthorizationPolicy"):
                selected = json.dumps(item["spec"].get("podSelector") or item["spec"].get("selector") or {})
                self.assertNotIn("mcp", selected, item["metadata"]["name"])
        flows = next(i for i in items if i["kind"] == "ConfigMap" and i["metadata"]["name"] == "llm-agent-network-flows")
        unresolved = {item["name"]: item for item in json.loads(flows["data"]["unresolved.json"])}
        self.assertIn("MCP Gateway", unresolved["agents-to-mcp-gateway"]["reason"])

    def test_security_settings_default_to_deny(self):
        def env_of(items, name):
            deployment = next(i for i in items if i["kind"] == "Deployment" and i["metadata"]["name"] == name)
            return {e["name"]: e["value"] for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}

        env = env_of(self.render(ENABLED), "drift-mcp")
        self.assertEqual(json.loads(env["EWALLET_MCP_TOOL_GRANTS"]), {})
        self.assertEqual((env["EWALLET_MCP_ALLOWED_ORIGINS"], env["EWALLET_MCP_MAX_BODY_BYTES"],
                          env["EWALLET_MCP_MAX_INFLIGHT"]), ("", "65536", "32"))
        granted = _merge(ENABLED, {"mcpServices": {
            "http": {"allowedOrigins": ["https://agents.example.invalid"]},
            "services": {"drift-mcp": {"toolGrants": {"spiffe-test/drift-specialist": ["drift_detection"]}}}}})
        env = env_of(self.render(granted), "drift-mcp")
        self.assertEqual(json.loads(env["EWALLET_MCP_TOOL_GRANTS"]), {"spiffe-test/drift-specialist": ["drift_detection"]})
        self.assertEqual(env["EWALLET_MCP_ALLOWED_ORIGINS"], "https://agents.example.invalid")

    def test_enabled_render_requires_real_identity(self):
        cases = {
            "tag instead of digest": {"mcpServices": {"image": "example.invalid/mcp:latest"}},
            "digest mismatch": {"mcpServices": {"identity": {"imageDigest": "sha256:" + "d" * 64}}},
            "short git sha": {"mcpServices": {"identity": {"gitSha": "abc123"}}},
            "missing model revision": {"mcpServices": {"identity": {"modelRevision": ""}}},
            "missing KServe URL": {"mcpServices": {"services": {"feature-rag-mcp": {"env": {"EWALLET_KSERVE_BASE_URL": ""}}}}},
        }
        for label, override in cases.items():
            with self.subTest(label):
                proc = self.render(_merge(ENABLED, override), check=False)
                self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
