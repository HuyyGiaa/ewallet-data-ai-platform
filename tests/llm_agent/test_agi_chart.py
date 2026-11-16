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
                "--set-string", "mcpServices.services.feature-rag-mcp.env.EWALLET_FRAUD_ALLOW_PRODUCTION_DB=true",
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
    def catalog(self, *overlays):
        import subprocess
        import sys

        command = [sys.executable, "-m", "ops.llm_agent.registry_catalog", "render", "--git-sha", "c" * 40]
        for overlay in overlays:
            command += ["--values-overlay", str(overlay)]
        out = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [doc for doc in yaml.safe_load_all(out) if doc]

    def agents(self, *overlays):
        return {doc["metadata"]["name"]: doc for doc in self.catalog(*overlays) if doc["kind"] == "Agent"}

    def test_catalog_describes_the_runtime_each_profile_deploys(self):
        import tempfile

        g3, agi = CHART / "g3/g3.yaml", CHART / "agi/agents.yaml"
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as production:
            # Recorded production: BYO Coordinator and Feature-RAG, Declarative Drift (also the rollback profile).
            yaml.safe_dump({"agents": {"coordinator": {"byo": {"enabled": True}},
                                       "specialists": {"feature-rag-specialist": {"byo": {"enabled": True}}}}}, production)
        try:
            cases = {
                "AGI": ((g3, agi), {"fraud-coordinator": ("kagent-byo", "1.1.0"),
                                    "feature-rag-specialist": ("kagent-byo", "1.0.0"),
                                    "drift-specialist": ("kagent-byo", "1.1.0")}),
                "production / rollback": ((g3, Path(production.name)), {
                    "fraud-coordinator": ("kagent-byo", "1.0.0"), "feature-rag-specialist": ("kagent-byo", "1.0.0"),
                    "drift-specialist": ("kagent-declarative", "1.0.0")}),
                "chart defaults (agents.mode=sandbox)": ((), {name: ("kagent-sandboxagent", "1.0.0") for name in
                                                             ("fraud-coordinator", "feature-rag-specialist", "drift-specialist")}),
            }
            for label, (overlays, expected) in cases.items():
                with self.subTest(label):
                    agents = self.agents(*overlays)
                    for name, (runtime, version) in expected.items():
                        metadata, description = agents[name]["metadata"], agents[name]["spec"]["description"]
                        self.assertEqual((metadata["labels"]["ewallet.io/runtime"], metadata["tag"]), (runtime, version))
                        if runtime != "kagent-sandboxagent":
                            self.assertNotIn("Sandbox", description)
                        self.assertIn({"kagent-byo": "BYO", "kagent-declarative": "Declarative Agent",
                                       "kagent-sandboxagent": "SandboxAgent"}[runtime], description)
        finally:
            Path(production.name).unlink()

    def test_byo_agents_reference_only_the_mcp_server_they_call(self):
        agents = self.agents(CHART / "g3/g3.yaml", CHART / "agi/agents.yaml")
        servers = {name: [item["name"] for item in doc["spec"]["mcpServers"]] for name, doc in agents.items()}
        self.assertEqual(servers, {"drift-specialist": ["drift-mcp"], "feature-rag-specialist": ["feature-rag-mcp"],
                                   "fraud-coordinator": []})
        self.assertEqual(agents["fraud-coordinator"]["metadata"]["labels"]["ewallet.io/workflows"], "fraud.drift")

    def test_agi_catalog_matches_the_registry_schema(self):
        import json

        from ops.llm_agent.registry_catalog import schema_errors

        schemas = json.loads((ROOT / "infra/helm/agentregistry/openapi-subset-v0.4.0.json").read_text())["schemas"]
        for doc in self.catalog(CHART / "g3/g3.yaml", CHART / "agi/agents.yaml"):
            with self.subTest(kind=doc["kind"], name=doc["metadata"]["name"]):
                self.assertEqual(schema_errors(doc, schemas[doc["kind"]], schemas), [])

    def test_overlay_merge_replaces_lists_and_merges_maps(self):
        from ops.llm_agent.registry_catalog import merge_values

        merged = merge_values({"a": {"b": 1, "c": [1]}, "d": 1}, {"a": {"c": [2]}, "e": 2})
        self.assertEqual(merged, {"a": {"b": 1, "c": [2]}, "d": 1, "e": 2})


if __name__ == "__main__":
    unittest.main()


SANDBOX_ROUTES = ("--set", "a2aGateway.sandboxRoutes.namespace=ewallet-sandbox",
                  "--set", "a2aGateway.sandboxRoutes.agents={drift-specialist,feature-rag-specialist}",
                  "--set", "agents.coordinator.byo.sandboxSpecialists={drift-specialist,feature-rag-specialist}")


@unittest.skipUnless(HELM, "helm binary unavailable")
class SpecialistCleanupTests(unittest.TestCase):
    """Legacy Specialist Deployment cleanup: runtime: sandbox drops their BYO infrastructure only."""

    @classmethod
    def setUpClass(cls):
        cls.before = objects(helm("llm-agent", *AGENTS, *AGI_AGENTS, *SANDBOX_ROUTES))
        cls.after = objects(helm("llm-agent", *AGENTS, *AGI_AGENTS, *SANDBOX_ROUTES, "-f", "agi/specialists-sandbox-only.yaml"))

    def test_the_specialist_deployment_infrastructure_is_gone(self):
        gone = set(self.before) - set(self.after)
        self.assertEqual(set(self.after) - set(self.before), set(), "nothing is added")
        for name in ("drift-specialist", "feature-rag-specialist"):
            self.assertIn(("Agent", name), gone)
            self.assertIn(("ServiceAccount", name), gone)
            self.assertNotIn(("Agent", name), self.after)
        self.assertFalse([k for k in self.after if k[0] in ("PodDisruptionBudget", "ScaledObject", "HorizontalPodAutoscaler",
                                                           "AuthorizationPolicy", "NetworkPolicy", "ServiceAccount")
                          and ("drift-specialist" in k[1] or "feature-rag-specialist" in k[1])])

    def test_the_coordinator_keeps_its_runtime_and_sandbox_specialists(self):
        b = self.before[("Agent", "fraud-coordinator")]["spec"]["byo"]["deployment"]
        a = self.after[("Agent", "fraud-coordinator")]["spec"]["byo"]["deployment"]
        self.assertEqual((a["image"], env(a), a.get("args")), (b["image"], env(b), b.get("args")))
        self.assertEqual(env(a)["EWALLET_COORDINATOR_WORKFLOWS"], "fraud,drift")
        changed = {k for k in set(a.get("annotations") or {}) | set(b.get("annotations") or {})
                   if (a.get("annotations") or {}).get(k) != (b.get("annotations") or {}).get(k)}
        self.assertEqual(changed, {"ewallet.io/a2a-gateway-config-sha256"}, "only the gateway config rollout trigger")

    def test_a2a_gateway_keeps_exactly_the_sandbox_routes(self):
        def routes(objs):
            config = yaml.safe_load(objs[("ConfigMap", "a2a-gateway-config")]["data"]["config.yaml"])
            return [r["name"] for r in config["routes"]]
        self.assertIn("drift-specialist", routes(self.before))
        self.assertEqual([r for r in routes(self.after) if "specialist" in r],
                         ["sandbox-drift-specialist", "sandbox-feature-rag-specialist"])

    def test_invalid_runtime_or_a_drift_workflow_without_any_drift_runtime_fails(self):
        bad = helm("llm-agent", *AGENTS, *AGI_AGENTS, "--set", "agents.specialists.drift-specialist.runtime=pod")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("runtime must be deployment or sandbox", bad.stderr)
        no_route = helm("llm-agent", *AGENTS, *AGI_AGENTS, "-f", "agi/specialists-sandbox-only.yaml")
        self.assertNotEqual(no_route.returncode, 0, "drift workflows need the drift Sandbox route when it is retired")
