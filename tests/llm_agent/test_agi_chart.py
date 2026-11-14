"""Agent integration: Helm profile for the AGI evidence campaign (render-only, no cluster)."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
HELM = shutil.which("helm")
DIGEST = "sha256:" + "a" * 64
BYO_IMAGE = "asia-southeast1-docker.pkg.dev/p/r/agent@sha256:" + "b" * 64
COMMON = ("--set", f"vllm.image=r.example/p/vllm@{DIGEST}", "--set", f"mcpServices.image=r.example/p/mcp@{DIGEST}",
          "--set", f"mcpServices.identity.imageDigest={DIGEST}", "--set", "mcpServices.identity.gitSha=" + "c" * 40)
AGENTS = ("-f", "values-gke-cpu.yaml", "-f", "values-gke-cpu-g1.yaml", "-f", "g3/g3.yaml",
          "-f", "n11/values-n11-agents.yaml", "--set", "idempotencyRedis.enabled=true",
          "--set", "otelCollector.enabled=true")
AGI_AGENTS = ("-f", "agi/agents.yaml", "--set", f"agents.coordinator.byo.image={BYO_IMAGE}",
              "--set", f"agents.specialists.feature-rag-specialist.byo.image={BYO_IMAGE}",
              "--set", f"agents.specialists.drift-specialist.byo.image={BYO_IMAGE}",
              "--set", "modelConfig.byoResolver.apiServer.cidrs={10.0.0.1/32}")
MCP = ("-n", "ewallet-mcp", "-f", "values-gke-cpu.yaml", "-f", "g3/g3.yaml", "-f", "g3/mcp-isolated.yaml")
AGI_MCP_SETS = ("--set", "mcpServices.identity.configRevision=agi-20261115T000000Z",
                "--set-string", "mcpServices.services.feature-rag-mcp.env.EWALLET_FRAUD_ONLINE_DB=0",
                "--set-string", "mcpServices.services.feature-rag-mcp.env.EWALLET_RAG_ONLINE_DB=3",
                "--set", "mcpServices.services.drift-mcp.env.EWALLET_DRIFT_BASELINES_SHA256=" + "d" * 64,
                "--set", "mcpServices.services.drift-mcp.env.EWALLET_DRIFT_WINDOW_MANIFEST_SHA256=" + "e" * 64,
                "--set", "mcpServices.services.drift-mcp.env.EWALLET_DRIFT_DATASET_REVISION=feast-r5gb-20261111-214544")


def helm(release: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([HELM, "template", release, str(CHART), *args, *COMMON], cwd=CHART,
                          capture_output=True, text=True)


def objects(result: subprocess.CompletedProcess) -> dict[tuple[str, str], dict]:
    assert result.returncode == 0, result.stderr
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in yaml.safe_load_all(result.stdout) if doc}


def env(container_or_byo: dict) -> dict[str, object]:
    return {item["name"]: item.get("value", item.get("valueFrom")) for item in container_or_byo.get("env", [])}


@unittest.skipUnless(HELM, "helm binary unavailable")
class AgentProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.default = objects(helm("llm-agent", *AGENTS, "--set", "agents.coordinator.byo.enabled=true",
                                   "--set", "agents.specialists.feature-rag-specialist.byo.enabled=true"))
        cls.agi = objects(helm("llm-agent", *AGENTS, *AGI_AGENTS))

    def test_default_profile_keeps_declarative_drift_and_fraud_only_coordinator(self):
        drift = self.default[("Agent", "drift-specialist")]["spec"]
        self.assertEqual((drift["type"], drift["declarative"]["modelConfig"]), ("Declarative", "ewallet-global-llm"))
        coordinator = env(self.default[("Agent", "fraud-coordinator")]["spec"]["byo"]["deployment"])
        self.assertNotIn("EWALLET_COORDINATOR_WORKFLOWS", coordinator)

    def test_agi_profile_changes_only_the_three_agents_and_adds_the_resolver(self):
        changed = {key for key in self.agi if key in self.default and self.agi[key] != self.default[key]}
        added = set(self.agi) - set(self.default)
        self.assertEqual(changed, {("Agent", "fraud-coordinator"), ("Agent", "feature-rag-specialist"),
                                   ("Agent", "drift-specialist")})
        self.assertEqual(added, {("Role", "ewallet-global-llm-reader"), ("RoleBinding", "ewallet-global-llm-reader"),
                                 ("NetworkPolicy", "allow-byo-modelconfig-to-kube-apiserver")})
        self.assertEqual(set(self.default) - set(self.agi), set())

    def test_drift_specialist_is_deterministic_byo_on_its_own_mcp_credential(self):
        spec = self.agi[("Agent", "drift-specialist")]["spec"]
        deployment = spec["byo"]["deployment"]
        self.assertEqual((spec["type"], deployment["args"]), ("BYO", ["ai_platform.llm_agent.byo_drift_specialist"]))
        variables = env(deployment)
        self.assertEqual(variables["EWALLET_MCP_SERVICE_TOKEN"]["secretKeyRef"]["name"],
                         "mcp-token-drift-specialist-drift-mcp")
        self.assertFalse({"EWALLET_NL_INTAKE_ENABLED", "EWALLET_LLM_API_KEY", "EWALLET_LLM_CONFIG_SOURCE"} & set(variables))
        self.assertEqual(deployment["serviceAccountName"], "drift-specialist")

    def test_coordinator_uses_the_global_modelconfig_and_both_workflows(self):
        variables = env(self.agi[("Agent", "fraud-coordinator")]["spec"]["byo"]["deployment"])
        self.assertEqual(variables["EWALLET_COORDINATOR_WORKFLOWS"], "fraud,drift")
        self.assertEqual(variables["EWALLET_LLM_CONFIG_SOURCE"], "modelconfig")
        self.assertEqual(variables["EWALLET_LLM_MODELCONFIG_NAME"], "ewallet-global-llm")
        self.assertNotIn("EWALLET_LLM_BASE_URL", variables)
        role = self.agi[("Role", "ewallet-global-llm-reader")]["rules"][0]
        self.assertEqual((role["resourceNames"], role["verbs"]), (["ewallet-global-llm"], ["get"]))
        subjects = {item["name"] for item in self.agi[("RoleBinding", "ewallet-global-llm-reader")]["subjects"]}
        self.assertEqual(subjects, {"fraud-coordinator", "feature-rag-specialist"})

    def test_gateways_route_both_specialists_and_nothing_else(self):
        a2a = yaml.safe_load(self.agi[("ConfigMap", "a2a-gateway-config")]["data"]["config.yaml"])
        hosts = {route["name"]: route["matches"][0]["headers"][0]["value"]["exact"]
                 for route in a2a["routes"] if route["name"] != "kagent-a2a"}
        self.assertEqual(hosts, {"drift-specialist": "drift-specialist.ewallet-agents",
                                 "feature-rag-specialist": "feature-rag-specialist.ewallet-agents"})
        mcp = yaml.safe_load(self.agi[("ConfigMap", "mcp-gateway-config")]["data"]["config.yaml"])
        paths = {route["name"]: route["matches"][0]["path"]["pathPrefix"] for route in mcp["routes"]}
        self.assertEqual(paths, {"drift-mcp": "/drift/mcp", "feature-rag-mcp": "/feature-rag/mcp"})

    def test_unsafe_combinations_refuse_to_render(self):
        cases = {
            "drift workflows without the BYO drift agent": (
                "--set", "agents.specialists.drift-specialist.byo.enabled=false"),
            "drift agent with LLM intake": (
                "--set", "agents.specialists.drift-specialist.byo.naturalLanguageIntake.enabled=true"),
            "drift agent image by tag": (
                "--set", "agents.specialists.drift-specialist.byo.image=asia-southeast1-docker.pkg.dev/p/r/a:latest"),
            "unknown workflow": ("--set", "agents.coordinator.byo.workflows={fraud,drift,payments}"),
            "fraud missing": ("--set", "agents.coordinator.byo.workflows={drift}"),
            "unknown only": ("--set", "agents.coordinator.byo.workflows={fraud,payments}"),
        }
        for label, extra in cases.items():
            with self.subTest(label):
                self.assertNotEqual(helm("llm-agent", *AGENTS, *AGI_AGENTS, *extra).returncode, 0)
        missing_cidrs = [arg for arg in AGI_AGENTS if "cidrs" not in arg]
        self.assertNotEqual(helm("llm-agent", *AGENTS, *missing_cidrs[:-1]).returncode, 0)


@unittest.skipUnless(HELM, "helm binary unavailable")
class McpProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.default = objects(helm("llm-agent-mcp", *MCP))
        cls.agi = objects(helm("llm-agent-mcp", *MCP, "-f", "agi/mcp.yaml", *AGI_MCP_SETS))

    def container(self, docs, name):
        return docs[("Deployment", name)]["spec"]["template"]["spec"]["containers"][0]

    def test_only_the_two_mcp_deployments_change(self):
        changed = {key for key in self.agi if self.agi[key] != self.default.get(key)}
        self.assertEqual(changed, {("Deployment", "feature-rag-mcp"), ("Deployment", "drift-mcp")})

    def test_feature_rag_uses_two_independent_pinned_targets(self):
        variables = env(self.container(self.agi, "feature-rag-mcp"))
        self.assertEqual(variables["EWALLET_FEATURE_TARGET_PROFILE"], "isolated")
        self.assertEqual(variables["EWALLET_REDIS_CONNECTION_STRING"]["secretKeyRef"]["name"],
                         "feast-online-redis-agi-fraud")
        self.assertEqual(variables["EWALLET_RAG_REDIS_CONNECTION_STRING"]["secretKeyRef"]["name"],
                         "feast-online-redis-agi-rag")
        self.assertEqual((variables["EWALLET_FRAUD_ONLINE_DB"], variables["EWALLET_RAG_ONLINE_DB"]), ("0", "3"))
        self.assertEqual(variables["EWALLET_RAG_FEAST_PROJECT"], "ewallet_rag_hybrid_20261114t144825")
        default = env(self.container(self.default, "feature-rag-mcp"))
        self.assertNotIn("EWALLET_RAG_REDIS_CONNECTION_STRING", default)

    def test_drift_mcp_mounts_pinned_artifacts_read_only_and_refuses_synthetic_data(self):
        container = self.container(self.agi, "drift-mcp")
        variables = env(container)
        self.assertEqual((variables["EWALLET_DRIFT_SOURCE"], variables["EWALLET_DRIFT_ALLOW_SYNTHETIC"]),
                         ("artifact", "false"))
        mounts = {item["mountPath"]: item.get("readOnly") for item in container["volumeMounts"]}
        self.assertEqual((mounts["/drift/baselines"], mounts["/drift/window"]), (True, True))
        self.assertNotIn("EWALLET_DRIFT_SOURCE", env(self.container(self.default, "drift-mcp")))

    def test_unpinned_overlay_refuses_to_render(self):
        self.assertNotEqual(helm("llm-agent-mcp", *MCP, "-f", "agi/mcp.yaml").returncode, 0)
        integer_db = [item.replace("--set-string", "--set") for item in AGI_MCP_SETS]
        self.assertNotEqual(helm("llm-agent-mcp", *MCP, "-f", "agi/mcp.yaml", *integer_db).returncode, 0)


class RegistryOverlayTests(unittest.TestCase):
    def test_agi_overlay_publishes_new_versions_and_runtime_labels(self):
        import subprocess
        import sys

        out = subprocess.run([sys.executable, "-m", "ops.llm_agent.registry_catalog", "render", "--git-sha", "c" * 40,
                              "--values-overlay", str(CHART / "agi/agents.yaml")], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        agents = {doc["metadata"]["name"]: doc["metadata"] for doc in yaml.safe_load_all(out) if doc["kind"] == "Agent"}
        self.assertEqual((agents["drift-specialist"]["tag"], agents["fraud-coordinator"]["tag"]), ("1.1.0", "1.1.0"))
        self.assertEqual(agents["drift-specialist"]["labels"]["ewallet.io/runtime"], "kagent-byo")
        self.assertEqual(agents["fraud-coordinator"]["labels"]["ewallet.io/workflows"], "fraud.drift")

    def test_overlay_merge_replaces_lists_and_merges_maps(self):
        from ops.llm_agent.registry_catalog import merge_values

        merged = merge_values({"a": {"b": 1, "c": [1]}, "d": 1}, {"a": {"c": [2]}, "e": 2})
        self.assertEqual(merged, {"a": {"b": 1, "c": [2]}, "d": 1, "e": 2})


if __name__ == "__main__":
    unittest.main()
