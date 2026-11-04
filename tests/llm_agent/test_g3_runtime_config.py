"""G3 GKE profile: kagent Agent mode, gateways, scoped MLOps access, runtime inputs."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops.llm_agent import g3_mcp_credentials as creds  # noqa: E402
from ops.llm_agent import g3_platform_inputs as inputs  # noqa: E402

sys.path.insert(0, str(ROOT / "infra/kubernetes/kagent-platform"))
import postrender_g3_placement as postrender  # noqa: E402

CHART = ROOT / "infra/helm/llm-agent-platform"
HELM = shutil.which("helm")
VLLM = "r.example/p/vllm-cpu-model@sha256:" + "a" * 64
MCP_DIGEST = "sha256:" + "b" * 64
COORDINATOR_IMAGE = (
    "asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/"
    "ewallet-platform/g3-byo-coordinator@sha256:" + "d" * 64
)
MCP_NAMESPACE = ROOT / "infra/kubernetes/llm-agent-mcp/namespace.yaml"
REVISION3_NULL_SERVICE = ROOT / "tests/llm_agent/fixtures/llm-agent-revision3-null-service.yaml"


def render(*extra: str, release: str = "llm-agent", profiles: tuple[str, ...] = (),
           chart: Path = CHART) -> subprocess.CompletedProcess:
    command = [HELM, "template", release, str(chart), "-f", str(chart / "values-gke-cpu.yaml"),
               "-f", str(chart / "values-gke-cpu-g1.yaml"), "-f", str(chart / "g3/g3.yaml")]
    for profile in profiles:
        command.extend(("-f", str(chart / "g3" / profile)))
    command.extend(("--set", f"vllm.image={VLLM}",
                    "--set", f"mcpServices.image=r.example/p/mcp-tools@{MCP_DIGEST}",
                    "--set", f"mcpServices.identity.imageDigest={MCP_DIGEST}",
                    "--set", "mcpServices.identity.gitSha=" + "c" * 40))
    return subprocess.run([*command, *extra], capture_output=True, text=True)


@unittest.skipUnless(HELM, "helm binary unavailable")
class G3ChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = render()
        assert out.returncode == 0, out.stderr
        cls.docs = [d for d in yaml.safe_load_all(out.stdout) if d]

    def kinds(self, kind):
        return {d["metadata"]["name"]: d for d in self.docs if d["kind"] == kind}

    def test_durable_idempotency_opt_in_is_a_dedicated_g3_contract(self):
        out = render(
            "--set", "agents.coordinator.byo.enabled=true",
            "--set", f"agents.coordinator.byo.image={COORDINATOR_IMAGE}",
            "--set", "idempotencyRedis.enabled=true",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        docs = [doc for doc in yaml.safe_load_all(out.stdout) if doc]
        stateful = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "StatefulSet"}
        self.assertIn("coordinator-idempotency-redis", stateful)
        coordinator = next(
            doc for doc in docs
            if doc["kind"] == "Agent" and doc["metadata"]["name"] == "fraud-coordinator"
        )
        self.assertEqual(coordinator["spec"]["type"], "BYO")
        env_names = {
            item["name"] for item in coordinator["spec"]["byo"]["deployment"]["env"]
        }
        self.assertIn("EWALLET_IDEMPOTENCY_REDIS_URL", env_names)

    def test_agent_mode_renders_kagent_agents_without_substrate(self):
        agents = self.kinds("Agent")
        self.assertEqual(sorted(agents), ["drift-specialist", "feature-rag-specialist", "fraud-coordinator"])
        self.assertFalse(self.kinds("SandboxAgent") or self.kinds("WorkerPool"))
        for name, agent in agents.items():
            deployment = agent["spec"]["declarative"]["deployment"]
            self.assertEqual(deployment["serviceAccountName"], name)       # own mesh identity
            self.assertEqual(deployment["nodeSelector"], {"ewallet.io/g3-llm": "true"})
            self.assertNotIn("substrate", agent["spec"])

    def test_coordinator_delegates_only_and_specialists_hold_least_privilege(self):
        agents = self.kinds("Agent")
        coordinator = agents["fraud-coordinator"]["spec"]["declarative"]["tools"]
        self.assertEqual({t["type"] for t in coordinator}, {"Agent"})       # no MCP business tool
        self.assertTrue(all(t["isolateSessions"] is True for t in coordinator))
        tools = {name: {t["mcpServer"]["name"]: t["mcpServer"]["toolNames"]
                        for t in agents[name]["spec"]["declarative"]["tools"]}
                 for name in ("drift-specialist", "feature-rag-specialist")}
        self.assertEqual(tools["drift-specialist"], {"feature-rag-mcp": ["feature_lookup"], "drift-mcp": ["drift_detection"]})
        self.assertEqual(tools["feature-rag-specialist"],
                         {"feature-rag-mcp": ["feature_lookup", "fraud_scoring", "knowledge_search"]})
        for server in self.kinds("RemoteMCPServer").values():                # only via the MCP Gateway
            self.assertTrue(server["spec"]["url"].startswith("http://mcp-gateway.ewallet-agents.svc:8080/"))

    def test_a2a_config_checksum_rolls_only_the_coordinator(self):
        agents = self.kinds("Agent")
        coordinator = agents["fraud-coordinator"]["spec"]["declarative"]["deployment"]
        checksum = coordinator["annotations"]["ewallet.io/a2a-gateway-config-sha256"]
        self.assertRegex(checksum, r"^[0-9a-f]{64}$")
        for name in ("drift-specialist", "feature-rag-specialist"):
            deployment = agents[name]["spec"]["declarative"]["deployment"]
            self.assertNotIn("ewallet.io/a2a-gateway-config-sha256",
                             deployment.get("annotations", {}))

        changed = render("--set", "a2aGateway.rateLimit.capacity=11")
        self.assertEqual(changed.returncode, 0, changed.stderr)
        changed_agents = {d["metadata"]["name"]: d for d in yaml.safe_load_all(changed.stdout)
                          if d and d["kind"] == "Agent"}
        changed_checksum = changed_agents["fraud-coordinator"]["spec"]["declarative"][
            "deployment"]["annotations"]["ewallet.io/a2a-gateway-config-sha256"]
        self.assertNotEqual(changed_checksum, checksum)

    def test_mcp_config_checksum_rolls_only_specialists(self):
        agents = self.kinds("Agent")
        annotation = "ewallet.io/mcp-gateway-config-sha256"
        checksums = {
            name: agents[name]["spec"]["declarative"]["deployment"]["annotations"][annotation]
            for name in ("drift-specialist", "feature-rag-specialist")
        }
        self.assertEqual(len(set(checksums.values())), 1)
        self.assertRegex(next(iter(checksums.values())), r"^[0-9a-f]{64}$")
        coordinator = agents["fraud-coordinator"]
        self.assertNotIn(annotation, coordinator["spec"]["declarative"]["deployment"]["annotations"])

        relevant = render("--set", "mcpGateway.rateLimit.capacity=21")
        self.assertEqual(relevant.returncode, 0, relevant.stderr)
        relevant_docs = [doc for doc in yaml.safe_load_all(relevant.stdout) if doc]
        relevant_agents = {doc["metadata"]["name"]: doc for doc in relevant_docs
                           if doc["kind"] == "Agent"}
        for name, checksum in checksums.items():
            changed = relevant_agents[name]["spec"]["declarative"]["deployment"][
                "annotations"][annotation]
            self.assertNotEqual(changed, checksum)
        self.assertEqual(relevant_agents["fraud-coordinator"], coordinator)

        unrelated = render("--set", "agents.resources.limits.memory=513Mi")
        self.assertEqual(unrelated.returncode, 0, unrelated.stderr)
        unrelated_agents = {doc["metadata"]["name"]: doc for doc in yaml.safe_load_all(unrelated.stdout)
                            if doc and doc["kind"] == "Agent"}
        for name, checksum in checksums.items():
            unchanged = unrelated_agents[name]["spec"]["declarative"]["deployment"][
                "annotations"][annotation]
            self.assertEqual(unchanged, checksum)

    def test_mlops_access_is_two_scoped_additive_policies(self):
        foreign = [d for d in self.docs if d["metadata"].get("namespace") not in ("ewallet-agents", None)]
        self.assertEqual({(d["kind"], d["metadata"]["namespace"]) for d in foreign},
                         {("NetworkPolicy", "feature-store"), ("NetworkPolicy", "fraud-serving")})
        for policy in foreign:
            (rule,) = policy["spec"]["ingress"]
            (source,) = rule["from"]
            self.assertEqual(source["podSelector"]["matchLabels"], {"app.kubernetes.io/name": "feature-rag-mcp"})
            self.assertEqual(source["namespaceSelector"]["matchLabels"], {"kubernetes.io/metadata.name": "ewallet-agents"})
            self.assertEqual(len(rule["ports"]), 1)

    def test_gateways_authorize_per_identity(self):
        maps = self.kinds("ConfigMap")
        mcp = yaml.safe_load(maps["mcp-gateway-config"]["data"]["config.yaml"])
        rules = {r["name"]: r["policies"]["mcpAuthorization"]["rules"] for r in mcp["routes"]}
        # Ambient ztunnel supplies the caller gate. The authenticated MCP
        # backend applies exact per-subject tool grants and list-only discovery.
        self.assertEqual(rules, {"feature-rag-mcp": ["true"], "drift-mcp": ["true"]})
        for route in mcp["routes"]:
            self.assertNotIn("transformations", route["policies"])
        a2a = yaml.safe_load(maps["a2a-gateway-config"]["data"]["config.yaml"])
        a2a_routes = {route["name"]: route for route in a2a["routes"]}
        for specialist in ("drift-specialist", "feature-rag-specialist"):
            route = a2a_routes[specialist]
            self.assertEqual(route["matches"], [{"headers": [{
                "name": "x-kagent-host",
                "value": {"exact": f"{specialist}.ewallet-agents"},
            }]}])
            self.assertEqual(route["policies"]["authorization"]["rules"], ["true"])
            self.assertEqual(route["backends"], [{
                "host": f"{specialist}.ewallet-agents.svc.cluster.local:8080",
            }])
        upstream = self.kinds("AuthorizationPolicy")["allow-fraud-coordinator-to-a2a-gateway"]
        (source,) = upstream["spec"]["rules"][0]["from"]
        self.assertEqual(source["source"]["principals"], [
            "cluster.local/ns/ewallet-agents/sa/fraud-coordinator",
        ])
        self.assertEqual(upstream["spec"]["rules"][0]["to"][0]["operation"]["ports"], ["8083"])
        self.assertEqual(a2a_routes["kagent-a2a"]["backends"], [
            {"host": "kagent-controller.kagent.svc.cluster.local:8083"},
        ])

    def test_kagent_controller_discovers_tools_with_its_own_list_only_credential(self):
        for server in self.kinds("RemoteMCPServer").values():
            (header,) = server["spec"]["headersFrom"]
            self.assertEqual(header["valueFrom"]["name"], f"mcp-token-kagent-controller-{server['metadata']['name']}")
        for name in ("feature-rag-mcp", "drift-mcp"):
            env = {e["name"]: e.get("value") for e in self.kinds("Deployment")[name]["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertEqual(json.loads(env["EWALLET_MCP_DISCOVERY_SUBJECTS"]), ["kagent-controller"])
            grants = json.loads(env["EWALLET_MCP_TOOL_GRANTS"])
            self.assertNotIn("kagent-controller", grants)                    # never a calling grant
        mcp = yaml.safe_load(self.kinds("ConfigMap")["mcp-gateway-config"]["data"]["config.yaml"])
        for route in mcp["routes"]:
            self.assertEqual(route["policies"]["mcpAuthorization"]["rules"], ["true"])
        controller_policy = self.kinds("AuthorizationPolicy")["allow-kagent-controller-to-mcp-gateway"]
        (source,) = controller_policy["spec"]["rules"][0]["from"]
        self.assertEqual(source["source"]["principals"], [
            "cluster.local/ns/kagent/sa/kagent-controller",
        ])

    def test_release_scoped_references_support_server_first_promotion_and_rollback(self):
        transition = render("--set", "mcpCredentialLifecycle.serverSet=r1-transition")
        promoted = render("--set", "mcpCredentialLifecycle.serverSet=r1-transition",
                          "--set", "mcpCredentialLifecycle.clientRelease=r1")
        rollback = render("--set", "mcpCredentialLifecycle.serverSet=legacy",
                          "--set", "mcpCredentialLifecycle.clientRelease=legacy")
        self.assertEqual((transition.returncode, promoted.returncode, rollback.returncode), (0, 0, 0))
        transition_docs = [d for d in yaml.safe_load_all(transition.stdout) if d]
        promoted_docs = [d for d in yaml.safe_load_all(promoted.stdout) if d]
        rollback_docs = [d for d in yaml.safe_load_all(rollback.stdout) if d]

        def objects(docs, kind):
            return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}

        transition_deployments = objects(transition_docs, "Deployment")
        promoted_deployments = objects(promoted_docs, "Deployment")
        rollback_deployments = objects(rollback_docs, "Deployment")
        for server in ("feature-rag-mcp", "drift-mcp"):
            env = transition_deployments[server]["spec"]["template"]["spec"]["containers"][0]["env"]
            auth = next(e for e in env if e["name"] == "EWALLET_MCP_SERVICE_TOKENS")
            self.assertEqual(auth["valueFrom"]["secretKeyRef"]["name"], f"mcp-auth-{server}-r1-transition")
            rollback_env = rollback_deployments[server]["spec"]["template"]["spec"]["containers"][0]["env"]
            rollback_auth = next(e for e in rollback_env if e["name"] == "EWALLET_MCP_SERVICE_TOKENS")
            self.assertEqual(rollback_auth["valueFrom"]["secretKeyRef"]["name"], f"mcp-auth-{server}")

        transition_agents = objects(transition_docs, "Agent")
        promoted_agents = objects(promoted_docs, "Agent")
        old_header = transition_agents["feature-rag-specialist"]["spec"]["declarative"]["tools"][0]["headersFrom"][0]
        new_header = promoted_agents["feature-rag-specialist"]["spec"]["declarative"]["tools"][0]["headersFrom"][0]
        self.assertNotIn("-r1", old_header["valueFrom"]["name"])
        self.assertTrue(new_header["valueFrom"]["name"].endswith("-r1"))

        transition_remote = objects(transition_docs, "RemoteMCPServer")
        promoted_remote = objects(promoted_docs, "RemoteMCPServer")
        rollback_remote = objects(rollback_docs, "RemoteMCPServer")
        for server in transition_remote:
            self.assertFalse(transition_remote[server]["spec"]["headersFrom"][0]["valueFrom"]["name"].endswith("-r1"))
            self.assertTrue(promoted_remote[server]["spec"]["headersFrom"][0]["valueFrom"]["name"].endswith("-r1"))
            self.assertEqual(rollback_remote[server], transition_remote[server])

        # Credential phases change references only, never network or identity.
        self.assertEqual(objects(transition_docs, "NetworkPolicy"), objects(promoted_docs, "NetworkPolicy"))
        self.assertEqual(objects(transition_docs, "ServiceAccount"), objects(promoted_docs, "ServiceAccount"))
        self.assertFalse(objects(transition_docs, "Secret") or objects(promoted_docs, "Secret"))
        self.assertFalse(objects(rollback_docs, "Secret"))
        self.assertNotIn("auth-config.json:", transition.stdout)

    def test_invalid_credential_identifiers_fail_render(self):
        for key in ("serverSet", "clientRelease"):
            with self.subTest(key=key):
                out = render("--set", f"mcpCredentialLifecycle.{key}=INVALID/value")
                self.assertNotEqual(out.returncode, 0)
                self.assertIn(f"mcpCredentialLifecycle.{key}", out.stderr)

    def test_feature_rag_mcp_gets_secrets_by_reference_and_registry_snapshots(self):
        deployment = self.kinds("Deployment")["feature-rag-mcp"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"] if "valueFrom" in e}
        self.assertEqual(refs["EWALLET_REDIS_CONNECTION_STRING"], {"name": "feast-online-redis", "key": "connection_string"})
        mounts = {m["mountPath"] for m in container["volumeMounts"]}
        self.assertTrue({"/feast/fraud", "/feast/rag"} <= mounts)
        self.assertNotIn("redis://", json.dumps(deployment))

    def test_mcp_and_agent_pdbs_keep_separate_availability_contracts(self):
        pdbs = self.kinds("PodDisruptionBudget")
        self.assertEqual(
            set(pdbs),
            {
                "drift-mcp",
                "drift-specialist",
                "feature-rag-specialist",
                "fraud-coordinator",
            },
        )
        self.assertNotIn("feature-rag-mcp", pdbs)
        self.assertEqual(pdbs["drift-mcp"]["spec"]["minAvailable"], 1)
        for agent in ("drift-specialist", "feature-rag-specialist", "fraud-coordinator"):
            with self.subTest(agent=agent):
                self.assertEqual(pdbs[agent]["spec"]["minAvailable"], 1)
                self.assertEqual(
                    pdbs[agent]["spec"]["selector"]["matchLabels"],
                    {"ewallet.io/agent": agent},
                )

    def test_isolated_profile_has_disjoint_mcp_only_ownership(self):
        isolated = render(release="llm-agent-mcp", profiles=("mcp-isolated.yaml",))
        self.assertEqual(isolated.returncode, 0, isolated.stderr)
        docs = [doc for doc in yaml.safe_load_all(isolated.stdout) if doc]
        local = [doc for doc in docs if doc.get("metadata", {}).get("namespace") == "ewallet-mcp"]
        names = {(doc["kind"], doc["metadata"]["name"]) for doc in local}
        self.assertEqual({name for kind, name in names if kind == "Deployment"},
                         {"feature-rag-mcp", "drift-mcp"})
        self.assertEqual({name for kind, name in names if kind == "Service"},
                         {"feature-rag-mcp", "drift-mcp"})
        self.assertEqual({name for kind, name in names if kind == "ServiceAccount"},
                         {"feature-rag-mcp", "drift-mcp"})
        self.assertFalse({"Agent", "RemoteMCPServer", "ModelConfig", "Role", "RoleBinding"} &
                         {kind for kind, _ in names})
        self.assertNotIn(("Deployment", "mcp-gateway"), names)
        self.assertEqual({name for kind, name in names if kind == "PodDisruptionBudget"}, {"drift-mcp"})
        self.assertFalse({doc["kind"] for doc in docs} & {
            "Namespace", "Secret", "ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition",
        })

        authorizations = {doc["metadata"]["name"]: doc for doc in local
                          if doc["kind"] == "AuthorizationPolicy"}
        for server in ("feature-rag-mcp", "drift-mcp"):
            policy = authorizations[f"allow-mcp-gateway-to-{server}"]
            self.assertEqual(policy["spec"]["rules"][0]["from"][0]["source"]["principals"],
                             ["cluster.local/ns/ewallet-agents/sa/mcp-gateway"])

        policies = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "NetworkPolicy"}
        for server in ("feature-rag-mcp", "drift-mcp"):
            policy = policies[f"allow-mcp-gateway-to-{server}-ingress"]
            source = policy["spec"]["ingress"][0]["from"][0]
            self.assertEqual(source["namespaceSelector"]["matchLabels"],
                             {"kubernetes.io/metadata.name": "ewallet-agents"})
            self.assertEqual(source["podSelector"]["matchLabels"],
                             {"app.kubernetes.io/name": "mcp-gateway"})
        self.assertNotIn("allow-drift-mcp-to-feast-redis-egress", policies)
        self.assertNotIn("allow-drift-mcp-to-kserve-fraud-detector-egress", policies)

        for namespace in ("feature-store", "fraud-serving"):
            (policy,) = [doc for doc in docs if doc["kind"] == "NetworkPolicy" and
                         doc["metadata"].get("namespace") == namespace]
            source = policy["spec"]["ingress"][0]["from"][0]
            self.assertEqual(source["namespaceSelector"]["matchLabels"],
                             {"kubernetes.io/metadata.name": "ewallet-mcp"})
            self.assertEqual(source["podSelector"]["matchLabels"],
                             {"app.kubernetes.io/name": "feature-rag-mcp"})

        legacy_ids = {(doc["kind"], doc["metadata"].get("namespace"), doc["metadata"]["name"])
                      for doc in self.docs}
        isolated_ids = {(doc["kind"], doc["metadata"].get("namespace"), doc["metadata"]["name"])
                        for doc in docs}
        self.assertFalse(legacy_ids & isolated_ids)

    def test_isolated_namespace_has_an_external_lifecycle(self):
        namespace = yaml.safe_load(MCP_NAMESPACE.read_text())
        self.assertEqual((namespace["kind"], namespace["metadata"]["name"]),
                         ("Namespace", "ewallet-mcp"))
        self.assertEqual(namespace["metadata"]["labels"], {
            "app.kubernetes.io/part-of": "ewallet-llm-agent",
            "ewallet.io/lifecycle-owner": "platform-operator",
            "istio.io/dataplane-mode": "ambient",
            "pod-security.kubernetes.io/enforce": "baseline",
            "pod-security.kubernetes.io/warn": "restricted",
            "pod-security.kubernetes.io/audit": "restricted",
        })
        self.assertNotIn("annotations", namespace["metadata"])

    def test_cutover_changes_backends_but_preserves_legacy_workloads(self):
        cutover = render(profiles=("mcp-cutover.yaml",))
        self.assertEqual(cutover.returncode, 0, cutover.stderr)
        docs = [doc for doc in yaml.safe_load_all(cutover.stdout) if doc]
        deployments = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "Deployment"}
        services = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "Service"}
        for server in ("feature-rag-mcp", "drift-mcp"):
            self.assertIn(server, deployments)
            self.assertIn(server, services)
        gateway = next(doc for doc in docs if doc["kind"] == "ConfigMap" and
                       doc["metadata"]["name"] == "mcp-gateway-config")
        config = yaml.safe_load(gateway["data"]["config.yaml"])
        hosts = {target["mcp"]["host"] for route in config["routes"]
                 for backend in route["backends"] for target in backend["mcp"]["targets"]}
        self.assertEqual(hosts, {
            "http://drift-mcp.ewallet-mcp.svc.cluster.local:8080/mcp",
            "http://feature-rag-mcp.ewallet-mcp.svc.cluster.local:8080/mcp",
        })
        policies = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "NetworkPolicy"}
        for server in ("feature-rag-mcp", "drift-mcp"):
            policy = policies[f"allow-mcp-gateway-to-{server}-cross-namespace-egress"]
            peer = policy["spec"]["egress"][0]["to"][0]
            self.assertEqual(peer["namespaceSelector"]["matchLabels"],
                             {"kubernetes.io/metadata.name": "ewallet-mcp"})
            self.assertEqual(peer["podSelector"]["matchLabels"],
                             {"app.kubernetes.io/name": server})

    def test_revision3_null_service_cutover_skips_disabled_legacy_workload(self):
        cutover = render("-f", str(REVISION3_NULL_SERVICE), profiles=("mcp-cutover.yaml",))
        self.assertEqual(cutover.returncode, 0, cutover.stderr)
        docs = [doc for doc in yaml.safe_load_all(cutover.stdout) if doc]
        legacy_deployments = {doc["metadata"]["name"] for doc in docs
                              if doc["kind"] == "Deployment" and
                              doc["metadata"].get("namespace") == "ewallet-agents"}
        self.assertIn("drift-mcp", legacy_deployments)
        self.assertNotIn("feature-rag-mcp", legacy_deployments)
        gateway = next(doc for doc in docs if doc["kind"] == "ConfigMap" and
                       doc["metadata"]["name"] == "mcp-gateway-config")
        self.assertIn(".ewallet-mcp.svc.cluster.local", gateway["data"]["config.yaml"])

    def test_explicitly_disabled_service_is_not_rendered(self):
        disabled = render("--set", "mcpServices.services.feature-rag-mcp.enabled=false")
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        docs = [doc for doc in yaml.safe_load_all(disabled.stdout) if doc]
        local = {doc["metadata"]["name"] for doc in docs if doc["kind"] == "Deployment" and
                 doc["metadata"].get("namespace") == "ewallet-agents"}
        self.assertIn("drift-mcp", local)
        self.assertNotIn("feature-rag-mcp", local)

    def test_malformed_service_configuration_fails_closed(self):
        malformed = render("--set-string", "mcpServices.services.feature-rag-mcp=malformed")
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("must be a map, null, or omitted", malformed.stderr)
        invalid_enabled = render("--set-string", "mcpServices.services.feature-rag-mcp.enabled=yes")
        self.assertNotEqual(invalid_enabled.returncode, 0)
        self.assertIn("enabled must be a boolean", invalid_enabled.stderr)

    def test_missing_service_entry_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "chart"
            shutil.copytree(CHART, chart)
            for path in (chart / "values.yaml", chart / "g3/g3.yaml"):
                values = yaml.safe_load(path.read_text())
                values["mcpServices"]["services"].pop("feature-rag-mcp", None)
                path.write_text(yaml.safe_dump(values, sort_keys=False))
            missing = render(chart=chart)
        self.assertEqual(missing.returncode, 0, missing.stderr)
        docs = [doc for doc in yaml.safe_load_all(missing.stdout) if doc]
        local = {doc["metadata"]["name"] for doc in docs if doc["kind"] == "Deployment" and
                 doc["metadata"].get("namespace") == "ewallet-agents"}
        self.assertIn("drift-mcp", local)
        self.assertNotIn("feature-rag-mcp", local)

    def test_invalid_backend_namespace_fails_closed(self):
        invalid = render("--set", "mcpGateway.backendNamespace=INVALID/name")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("backendNamespace", invalid.stderr)

    def test_cross_namespace_ingress_requires_a_mesh_principal(self):
        bad = render("--set", "crossNamespace.flows[0].name=x", "--set", "crossNamespace.flows[0].direction=ingress",
                     "--set", "crossNamespace.flows[0].local=drift-mcp", "--set", "crossNamespace.flows[0].port=1",
                     "--set", "crossNamespace.flows[0].peer.namespace=other",
                     "--set", "crossNamespace.flows[0].peer.podLabels.a=b")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("SPIFFE principal", bad.stderr)

    def test_sandbox_mode_still_requires_snapshots(self):
        out = render("--set", "agents.mode=sandbox")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("snapshotsLocation", out.stderr)


class CredentialFixture:
    values = {"agents": {"specialists": {
        "drift-specialist": {"tools": {"feature-rag-mcp": ["feature_lookup"], "drift-mcp": ["drift_detection"]}},
        "feature-rag-specialist": {"tools": {"feature-rag-mcp": ["feature_lookup", "fraud_scoring"]}}}}}

    def setUp(self):
        counter = iter(range(1000))
        self.factory = lambda: f"token-{next(counter):04d}-" + "x" * 40

    def plan(self, release="r1", server_set="r1-transition", mode="transition", previous="legacy",
             client_namespace="ewallet-agents", server_namespace="ewallet-mcp",
             previous_server_namespace="ewallet-agents"):
        return creds.ReleasePlan(release, server_set, mode, previous, client_namespace,
                                 server_namespace, previous_server_namespace,
                                 creds.grants_from_values(self.values))

    def previous_tables(self):
        return {
            "feature-rag-mcp": {"version": 1, "credentials": [
                {"id": "drift.legacy", "subject": "drift-specialist",
                 "token": "legacy-drift-feature-" + "a" * 40, "status": "active"},
                {"id": "feature.legacy", "subject": "feature-rag-specialist",
                 "token": "legacy-feature-" + "b" * 40, "status": "active"},
            ]},
            "drift-mcp": {"version": 1, "credentials": [
                {"id": "drift.legacy", "subject": "drift-specialist",
                 "token": "legacy-drift-" + "c" * 40, "status": "active"},
            ]},
        }


class CredentialTests(CredentialFixture, unittest.TestCase):

    def test_secret_placement_separates_client_and_server_material(self):
        plan = self.plan()
        issued = creds.new_credentials(plan.grants, plan.release, token_factory=self.factory)
        manifests = creds.secret_manifests(plan, issued, self.previous_tables())
        for manifest in manifests:
            name = manifest["metadata"]["name"]
            expected = "ewallet-mcp" if name.startswith("mcp-auth-") else "ewallet-agents"
            self.assertEqual(manifest["metadata"]["namespace"], expected)
        self.assertEqual(plan.previous_server_namespace, "ewallet-agents")
        self.assertIn(creds.SecretIdentity("ewallet-mcp", "mcp-auth-drift-mcp-r1-transition"),
                      plan.expected_secret_identities)

    def test_one_distinct_token_per_granted_pair_plus_one_revoked_per_server(self):
        pairs = creds.grants_from_values(self.values)
        issued = creds.new_credentials(pairs, "r1", token_factory=self.factory)
        self.assertEqual(len({c.token for c in issued}), len(issued))
        self.assertEqual(sum(c.status == "active" for c in issued), 3)
        self.assertEqual(sum(c.status == "revoked" for c in issued), 2)
        tables = creds._table_entries(issued)
        self.assertEqual(sorted(tables), ["drift-mcp", "feature-rag-mcp"])
        self.assertEqual({c["subject"] for c in tables["feature-rag-mcp"]},
                         {"drift-specialist", "feature-rag-specialist"})

    def test_secrets_never_put_one_agent_token_in_another_agent_secret(self):
        plan = self.plan()
        issued = creds.new_credentials(plan.grants, plan.release, token_factory=self.factory)
        manifests = {m["metadata"]["name"]: m for m in creds.secret_manifests(
            plan, issued, self.previous_tables())}
        self.assertIn("mcp-token-drift-specialist-drift-mcp-r1", manifests)
        self.assertIn("mcp-token-revoked-drift-mcp-r1", manifests)
        header = manifests["mcp-token-feature-rag-specialist-feature-rag-mcp-r1"]["stringData"]["token"]
        owner = [c for c in issued if c.token == header][0]
        self.assertEqual((owner.subject, owner.status), ("feature-rag-specialist", "active"))
        self.assertTrue(all(m["immutable"] for m in manifests.values()))

    def test_revoked_pair_gets_no_header_secret(self):
        pairs = creds.grants_from_values(self.values)
        plan = self.plan()
        issued = creds.new_credentials(pairs, "r1", revoked=frozenset({("drift-specialist", "drift-mcp")}),
                                       token_factory=self.factory)
        names = {m["metadata"]["name"] for m in creds.secret_manifests(plan, issued, self.previous_tables())}
        self.assertNotIn("mcp-token-drift-specialist-drift-mcp-r1", names)

    def test_transition_tables_accept_old_and_new_without_broadening_subjects(self):
        plan = self.plan()
        issued = creds.new_credentials(plan.grants, plan.release, token_factory=self.factory)
        manifests = {m["metadata"]["name"]: m for m in creds.secret_manifests(
            plan, issued, self.previous_tables())}
        table = json.loads(manifests["mcp-auth-feature-rag-mcp-r1-transition"]["stringData"]["auth-config.json"])
        ids = {item["id"] for item in table["credentials"]}
        self.assertTrue({"drift.legacy", "feature.legacy"} <= ids)
        self.assertTrue(any(".r1.active" in item for item in ids))
        self.assertEqual({item["subject"] for item in table["credentials"]},
                         {"drift-specialist", "feature-rag-specialist"})

    def test_final_set_contains_only_current_release(self):
        plan = self.plan(server_set="r1-final", mode="final", previous="r1-transition")
        issued = creds.new_credentials(plan.grants, plan.release, token_factory=self.factory)
        manifests = {m["metadata"]["name"]: m for m in creds.secret_manifests(plan, issued)}
        table = json.loads(manifests["mcp-auth-feature-rag-mcp-r1-final"]["stringData"]["auth-config.json"])
        self.assertTrue(all(".r1." in item["id"] for item in table["credentials"]))

    def test_invalid_previous_table_fails_closed_without_rendering_a_token(self):
        plan = self.plan()
        issued = creds.new_credentials(plan.grants, plan.release, token_factory=self.factory)
        previous = self.previous_tables()
        previous["feature-rag-mcp"]["credentials"][0]["subject"] = "fraud-coordinator"
        with self.assertRaisesRegex(ValueError, "failed validation") as caught:
            creds.secret_manifests(plan, issued, previous)
        self.assertNotIn("legacy-drift-feature", str(caught.exception))

    def test_release_identity_is_bounded_and_not_token_material(self):
        for value in ("UPPER", "has/slash", "x" * 25, "-bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.plan(release=value)
        issued = creds.new_credentials(self.plan().grants, "r1", token_factory=self.factory)
        self.assertTrue(all("r1" not in item.token for item in issued))


class MemorySecretStore:
    def __init__(self, previous):
        self.annotations = {}
        self.tables = {("ewallet-agents", creds.auth_secret_name(server, "legacy")): table
                       for server, table in previous.items()}
        self.created = []

    def metadata(self, namespace, names):
        return {name: self.annotations[(namespace, name)] for name in names
                if (namespace, name) in self.annotations}

    def auth_table(self, namespace, name):
        return self.tables[(namespace, name)]

    def create(self, manifests):
        self.created.append(manifests)
        for manifest in manifests:
            name = manifest["metadata"]["name"]
            namespace = manifest["metadata"]["namespace"]
            if (namespace, name) in self.annotations:
                raise AssertionError("existing Secret mutated")
            self.annotations[(namespace, name)] = manifest["metadata"]["annotations"]
            if "auth-config.json" in manifest["stringData"]:
                self.tables[(manifest["metadata"]["namespace"], name)] = json.loads(
                    manifest["stringData"]["auth-config.json"])


class CredentialPreparationTests(CredentialFixture, unittest.TestCase):
    def test_initial_and_repeated_preparation_are_create_then_noop(self):
        plan = self.plan()
        store = MemorySecretStore(self.previous_tables())
        self.assertEqual(creds.prepare_release(plan, store, token_factory=self.factory), "created")
        snapshot = dict(store.annotations)
        self.assertEqual(creds.prepare_release(plan, store, token_factory=lambda: self.fail("regenerated")),
                         "unchanged")
        self.assertEqual(store.annotations, snapshot)
        self.assertEqual(len(store.created), 1)

    def test_new_release_coexists_and_never_mutates_old_release(self):
        first = self.plan()
        store = MemorySecretStore(self.previous_tables())
        creds.prepare_release(first, store, token_factory=self.factory)
        old_snapshot = dict(store.annotations)
        second = self.plan(release="r2", server_set="r2-transition")
        self.assertEqual(creds.prepare_release(second, store, token_factory=self.factory), "created")
        for name, annotations in old_snapshot.items():
            self.assertEqual(store.annotations[name], annotations)
        self.assertTrue(set(first.expected_secret_names).isdisjoint(second.expected_secret_names))

    def test_partial_or_conflicting_release_fails_without_creation(self):
        plan = self.plan()
        store = MemorySecretStore(self.previous_tables())
        identity = plan.expected_secret_identities[0]
        store.annotations[(identity.namespace, identity.name)] = {
            creds.FINGERPRINT_ANNOTATION: plan.fingerprint
        }
        with self.assertRaisesRegex(RuntimeError, "partial"):
            creds.prepare_release(plan, store, token_factory=self.factory)
        self.assertFalse(store.created)
        store.annotations = {(identity.namespace, identity.name): {
            creds.FINGERPRINT_ANNOTATION: "0" * 64
        } for identity in plan.expected_secret_identities}
        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            creds.prepare_release(plan, store, token_factory=self.factory)
        self.assertFalse(store.created)

    def test_final_set_reuses_release_tokens_and_keeps_transition_set(self):
        transition = self.plan()
        store = MemorySecretStore(self.previous_tables())
        creds.prepare_release(transition, store, token_factory=self.factory)
        transition_snapshot = dict(store.annotations)
        final = self.plan(server_set="r1-final", mode="final", previous="r1-transition",
                          previous_server_namespace="ewallet-mcp")
        self.assertEqual(creds.prepare_release(final, store, token_factory=lambda: self.fail("regenerated")),
                         "created")
        for name, annotations in transition_snapshot.items():
            self.assertEqual(store.annotations[name], annotations)
        final_table = store.tables[("ewallet-mcp", "mcp-auth-feature-rag-mcp-r1-final")]
        self.assertTrue(all(".r1." in item["id"] for item in final_table["credentials"]))


class PlatformInputTests(unittest.TestCase):
    def test_replicated_secret_keeps_data_only(self):
        source = {"type": "Opaque", "data": {"connection_string": "eA=="},
                  "metadata": {"name": "feast-online-redis", "namespace": "ml-platform", "uid": "1",
                               "resourceVersion": "9", "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{}"}}}
        out = inputs.replicated_secret(source, "ewallet-mcp")
        self.assertEqual(out["metadata"]["namespace"], "ewallet-mcp")
        self.assertEqual(set(out["metadata"]), {"name", "namespace", "labels"})
        self.assertEqual(out["data"], {"connection_string": "eA=="})

    def test_registry_configmap_is_immutable_and_digest_annotated(self):
        content = b"registry-bytes"
        cm = inputs.registry_configmap("feast-registry-x", content, "gs://b/o/registry.db", "ewallet-mcp")
        self.assertTrue(cm["immutable"])
        self.assertEqual(cm["metadata"]["annotations"]["ewallet.io/registry-sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(base64.b64decode(cm["binaryData"]["registry.db"]), content)
        with self.assertRaises(ValueError):
            inputs.registry_configmap("big", b"x" * (inputs.MAX_CONFIGMAP_BYTES + 1), "f")

    def test_destination_namespace_is_validated(self):
        with self.assertRaisesRegex(ValueError, "DNS label"):
            inputs.replicated_secret({"metadata": {"name": "x", "namespace": "source"}}, "bad/name")

    def test_secret_copy_requires_approval_before_reading_source(self):
        with mock.patch.object(inputs, "_kubectl") as kubectl, self.assertRaises(SystemExit):
            inputs.main([
                "replicate-secret", "--context", "test-context",
                "--source-namespace", "ml-platform",
                "--destination-namespace", "ewallet-mcp",
                "--name", "feast-online-redis",
            ])
        kubectl.assert_not_called()

    def test_invalid_destination_fails_before_reading_source(self):
        with mock.patch.object(inputs, "_kubectl") as kubectl, self.assertRaises(SystemExit):
            inputs.main([
                "replicate-secret", "--context", "test-context",
                "--source-namespace", "ml-platform",
                "--destination-namespace", "INVALID/name",
                "--name", "feast-online-redis", "--approve-sensitive-copy",
            ])
        kubectl.assert_not_called()


class PostRendererTests(unittest.TestCase):
    def test_adds_g3_selector_only_where_missing(self):
        docs = [{"kind": "Deployment", "spec": {"template": {"spec": {}}}},
                {"kind": "Deployment", "spec": {"template": {"spec": {"nodeSelector": {"a": "b"}}}}},
                {"kind": "Service", "spec": {}}]
        out = postrender.place(docs)
        self.assertEqual(out[0]["spec"]["template"]["spec"]["nodeSelector"], {"ewallet.io/g3-llm": "true"})
        self.assertEqual(out[1]["spec"]["template"]["spec"]["nodeSelector"], {"a": "b"})
        self.assertEqual(out[2], {"kind": "Service", "spec": {}})


if __name__ == "__main__":
    unittest.main()
