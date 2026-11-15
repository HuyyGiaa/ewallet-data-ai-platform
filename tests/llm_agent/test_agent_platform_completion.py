"""Agent platform completion (S1-S7): sandbox delegation, gateway routes, fleet, KEDA, CRD patch.

Render and contract tests only; no cluster. The B' control loop itself is exercised by
scripts/experiments/bprime_kind.sh on a local kind cluster.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
HELM = shutil.which("helm")
YAML = importlib.util.find_spec("yaml") is not None
DIGEST = "sha256:" + "a" * 64
IMAGE = "asia-southeast1-docker.pkg.dev/p/r/agent@sha256:" + "b" * 64
sys.path.insert(0, str(ROOT))

from ai_platform.llm_agent.business_request import G3WorkflowRequest, a2a_message_send, member_request_id  # noqa: E402
from ai_platform.llm_agent.programmatic_coordinator import GatewayPolicy  # noqa: E402
from tests.llm_agent.test_agi_coordinator_workflows import drift_request, fraud_request  # noqa: E402


class SandboxDelegationTests(unittest.TestCase):
    def test_sandbox_specialist_goes_through_the_per_agent_controller_route_with_its_context(self):
        policy = GatewayPolicy(sandbox_specialists=frozenset({"drift-specialist"}), sandbox_namespace="ewallet-sandbox")
        request = drift_request("wf-1.drift")
        call = policy.call_for(request)
        self.assertEqual(call.url, "http://a2a-gateway.ewallet-agents.svc.cluster.local:8083/api/a2a/ewallet-sandbox/drift-specialist/")
        self.assertNotIn("x-kagent-host", call.headers)
        self.assertNotIn("x-user-id", {key.lower() for key in call.headers}, "the Gateway, not the client, sets X-User-Id")
        message = call.payload["params"]["message"]
        self.assertEqual((message["contextId"], message["messageId"], call.payload["id"]), ("wf-1.drift",) * 3)

    def test_deployment_specialists_keep_the_exact_host_route(self):
        policy = GatewayPolicy(sandbox_specialists=frozenset({"drift-specialist"}), sandbox_namespace="ewallet-sandbox")
        call = policy.call_for(fraud_request("wf-1.fraud"))
        self.assertEqual(call.url, "http://a2a-gateway.ewallet-agents.svc.cluster.local:8083/")
        self.assertEqual(call.headers["x-kagent-host"], "feature-rag-specialist.ewallet-agents")
        self.assertNotIn("contextId", call.payload["params"]["message"])
        self.assertEqual(GatewayPolicy().call_for(drift_request("d")).headers["x-kagent-host"], "drift-specialist.ewallet-agents")

    def test_invalid_sandbox_configuration_is_refused(self):
        cases = {
            "unknown agent": dict(sandbox_specialists=frozenset({"payments-agent"}), sandbox_namespace="ewallet-sandbox"),
            "no namespace": dict(sandbox_specialists=frozenset({"drift-specialist"}), sandbox_namespace=""),
            "agents namespace": dict(sandbox_specialists=frozenset({"drift-specialist"}), sandbox_namespace="ewallet-agents"),
            "path injection": dict(sandbox_specialists=frozenset({"drift-specialist"}), sandbox_namespace="a/../b"),
        }
        for label, kwargs in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                GatewayPolicy(**kwargs)

    def test_from_env(self):
        policy = GatewayPolicy.from_env({"EWALLET_SANDBOX_SPECIALISTS": "drift-specialist, feature-rag-specialist",
                                         "EWALLET_SANDBOX_NAMESPACE": "ewallet-sandbox"}, timeout_seconds=30)
        self.assertEqual(policy.sandbox_specialists, frozenset({"drift-specialist", "feature-rag-specialist"}))
        self.assertEqual(GatewayPolicy.from_env({}, timeout_seconds=30).sandbox_specialists, frozenset())

    def test_combined_workflow_mixes_routes_and_keeps_member_contexts(self):
        from ai_platform.llm_agent.idempotency import IdempotencyScope, InMemoryIdempotencyStore
        from ai_platform.llm_agent.programmatic_coordinator import ProgrammaticCoordinator
        from tests.llm_agent.test_agi_coordinator_workflows import CoordinatorWorkflowTests, fraud_evidence

        CoordinatorWorkflowTests.setUpClass()
        try:
            helper = CoordinatorWorkflowTests()
            workflow = G3WorkflowRequest(request_id="wf-sbx", fraud=fraud_request(member_request_id("wf-sbx", "fraud")),
                                         drift=drift_request(member_request_id("wf-sbx", "drift")))
            calls = []

            def transport(call):
                calls.append(call)
                member = call.payload["id"]
                evidence = (helper.drift_evidence(member) if member.endswith(".drift") else fraud_evidence(member))
                from ai_platform.llm_agent.programmatic_coordinator import encode_specialist_task
                return encode_specialist_task(member, evidence)

            policy = GatewayPolicy(sandbox_specialists=frozenset({"drift-specialist", "feature-rag-specialist"}),
                                   sandbox_namespace="ewallet-sandbox")
            result = ProgrammaticCoordinator(transport=transport, synthesizer=lambda r, e: "ok", gateway=policy,
                                             idempotency_store=InMemoryIdempotencyStore()).handle(
                a2a_message_send(workflow), scope=IdempotencyScope("ewallet-agents", "kagent-controller"), session_id="s")
        finally:
            CoordinatorWorkflowTests.tearDownClass()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(sorted(call.url.rsplit("/", 2)[-2] for call in calls), ["drift-specialist", "feature-rag-specialist"])
        self.assertEqual(sorted(call.payload["params"]["message"]["contextId"] for call in calls),
                         ["wf-sbx.drift", "wf-sbx.fraud"])


@unittest.skipUnless(importlib.util.find_spec("a2a"), "agent runtime environment required")
class ShutdownGraceTests(unittest.TestCase):
    def test_grace_fits_inside_the_default_termination_period(self):
        from ai_platform.llm_agent.byo_runtime import shutdown_grace_seconds

        self.assertEqual(shutdown_grace_seconds({}), 25)
        for bad in ("0", "29", "abc"):
            with self.subTest(bad), self.assertRaises(ValueError):
                shutdown_grace_seconds({"EWALLET_SHUTDOWN_GRACE_SECONDS": bad})


@unittest.skipUnless(HELM and YAML, "helm and PyYAML required")
class ChartTests(unittest.TestCase):
    COMMON = ("--set", f"vllm.image=r.example/p/vllm@{DIGEST}", "--set", f"mcpServices.image=r.example/p/mcp@{DIGEST}",
              "--set", f"mcpServices.identity.imageDigest={DIGEST}", "--set", "mcpServices.identity.gitSha=" + "c" * 40)
    AGENTS = ("-f", "values-gke-cpu.yaml", "-f", "values-gke-cpu-g1.yaml", "-f", "g3/g3.yaml", "-f", "n11/values-n11-agents.yaml",
              "--set", "idempotencyRedis.enabled=true", "--set", "otelCollector.enabled=true", "-f", "agi/agents.yaml",
              "--set", f"agents.coordinator.byo.image={IMAGE}", "--set", f"agents.specialists.feature-rag-specialist.byo.image={IMAGE}",
              "--set", f"agents.specialists.drift-specialist.byo.image={IMAGE}",
              "--set", "modelConfig.byoResolver.apiServer.cidrs={10.0.0.1/32}")
    SANDBOX = ("--set", "a2aGateway.sandboxRoutes.namespace=ewallet-sandbox",
               "--set", "a2aGateway.sandboxRoutes.agents={drift-specialist,feature-rag-specialist}",
               "--set", "agents.coordinator.byo.sandboxSpecialists={drift-specialist,feature-rag-specialist}")

    def render(self, *args, check=True):
        result = subprocess.run([HELM, "template", "llm-agent", str(CHART), *args, *self.COMMON], cwd=CHART,
                                capture_output=True, text=True)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def docs(self, *args):
        import yaml
        return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(self.render(*args).stdout) if d}

    def test_sandbox_routes_are_exact_and_set_the_principal(self):
        import yaml
        docs = self.docs(*self.AGENTS, *self.SANDBOX)
        config = yaml.safe_load(docs[("ConfigMap", "a2a-gateway-config")]["data"]["config.yaml"])
        routes = {route["name"]: route for route in config["routes"]}
        for agent in ("drift-specialist", "feature-rag-specialist"):
            route = routes[f"sandbox-{agent}"]
            self.assertEqual(route["matches"][0]["path"]["pathPrefix"], f"/api/a2a/ewallet-sandbox/{agent}/")
            self.assertIn("x-user-id", route["policies"]["requestHeaderModifier"]["remove"])
            self.assertEqual(route["policies"]["transformations"]["request"]["set"]["x-user-id"], '"service:fraud-coordinator"')
            self.assertEqual(route["policies"]["authorization"]["rules"], ["true"])
        env = {item["name"]: item.get("value") for item in
               docs[("Agent", "fraud-coordinator")]["spec"]["byo"]["deployment"]["env"]}
        self.assertEqual((env["EWALLET_SANDBOX_SPECIALISTS"], env["EWALLET_SANDBOX_NAMESPACE"]),
                         ("drift-specialist,feature-rag-specialist", "ewallet-sandbox"))

    def test_sandbox_route_misconfiguration_refuses_to_render(self):
        cases = {
            "agents namespace": ("--set", "a2aGateway.sandboxRoutes.namespace=ewallet-agents"),
            "undeclared agent": ("--set", "a2aGateway.sandboxRoutes.agents={payments-agent}",
                                 "--set", "agents.coordinator.byo.sandboxSpecialists={payments-agent}"),
            "coordinator and gateway disagree": ("--set", "agents.coordinator.byo.sandboxSpecialists={drift-specialist}"),
            "bad principal": ("--set", "a2aGateway.sandboxRoutes.userId=a b"),
        }
        for label, extra in cases.items():
            with self.subTest(label):
                self.assertNotEqual(self.render(*self.AGENTS, *self.SANDBOX, *extra, check=False).returncode, 0)

    def test_agi_keda_overlay_scales_all_three_byo_agents_on_the_inflight_gauge(self):
        import yaml
        docs = self.docs(*self.AGENTS, "-f", "g3/keda.yaml", "-f", "agi/keda.yaml")
        scaled = {name: doc for (kind, name), doc in docs.items() if kind == "ScaledObject"}
        self.assertEqual(set(scaled), {"fraud-coordinator", "feature-rag-specialist", "drift-specialist"})
        for name, item in scaled.items():
            with self.subTest(name):
                trigger = item["spec"]["triggers"][0]
                self.assertEqual((trigger["type"], trigger["metadata"]["threshold"]), ("prometheus", "2"))
                self.assertIn(f'agent="{name}"', trigger["metadata"]["query"])
                self.assertEqual(item["metadata"]["annotations"]["scaledobject.keda.sh/transfer-hpa-ownership"], "true")
                self.assertEqual(item["spec"]["scaleTargetRef"], {"apiVersion": "apps/v1", "kind": "Deployment", "name": name})
        self.assertFalse([name for (kind, name) in docs if kind == "HorizontalPodAutoscaler"])
        collector = yaml.safe_load(docs[("ConfigMap", "otel-collector-config")]["data"]["config.yaml"])
        self.assertEqual(collector["exporters"]["prometheus"]["metric_expiration"], "60s")

    def test_explicit_inflight_trigger_needs_a_byo_agent(self):
        result = self.render(*self.AGENTS, "-f", "g3/keda.yaml", "-f", "agi/keda.yaml",
                             "--set", "agents.specialists.drift-specialist.byo.enabled=false", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inflight trigger needs the BYO runtime", result.stderr)


@unittest.skipUnless(YAML, "PyYAML required")
class FleetAndCrdPatchTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("render_sandbox", ROOT / "scripts/experiments/render_sandbox.py")
        self.renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.renderer)

    def test_fleet_shares_one_pool_with_scoped_credentials(self):
        import yaml
        text = self.renderer.render("fleet", "r/x@sha256:" + "c" * 64, "agi-20261115T050000Z", "sbx1")
        docs = [doc for doc in yaml.safe_load_all(text) if doc]
        agents = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "SandboxAgent"}
        self.assertEqual(set(agents), {"feature-rag-specialist", "drift-specialist"})
        secrets = set()
        for name, agent in agents.items():
            with self.subTest(name):
                spec = agent["spec"]
                self.assertEqual(spec["substrate"]["workerPoolRef"]["name"], "business-agents")
                self.assertEqual(spec["substrate"]["snapshotsConfig"]["location"],
                                 f"gs://ate-snapshots/ewallet-sandbox/agi-20261115T050000Z/{name}/")
                env = {item["name"]: item for item in spec["byo"]["deployment"]["env"]}
                self.assertEqual(env["EWALLET_AGENT_PORT"]["value"], "80")
                self.assertNotIn("EWALLET_LLM_API_KEY", env)
                self.assertNotIn("agentgateway.ewallet-agents.svc.cluster.local", spec["sandbox"]["network"]["allowedDomains"])
                ref = env["EWALLET_MCP_SERVICE_TOKEN"]["valueFrom"]["secretKeyRef"]
                self.assertEqual(ref["key"], "token")
                secrets.add(ref["name"])
        # The names g3_mcp_credentials gives client Secrets: mcp-token-<subject>-<server>-<release>.
        from ops.llm_agent.g3_mcp_credentials import token_secret_name
        self.assertEqual(secrets, {token_secret_name("sandbox-feature-rag", "feature-rag-mcp", "sbx1"),
                                   token_secret_name("sandbox-drift", "drift-mcp", "sbx1")})
        pool = yaml.safe_load_all((ROOT / "infra/kubernetes/agent-sandbox/namespace.yaml").read_text())
        pools = [doc for doc in pool if doc and doc["kind"] == "WorkerPool"]
        self.assertEqual((pools[0]["metadata"]["name"], pools[0]["spec"]["replicas"]), ("business-agents", 2))
        for bad in ("agi-20261115t050000z", "prod"):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.renderer.render("fleet", "r/x@sha256:" + "c" * 64, bad, "sbx1")
        with self.assertRaises(ValueError):
            self.renderer.render("fleet", "r/x:latest", "agi-20261115T050000Z", "sbx1")
        for bad in ("", "Sbx", "x" * 25):
            with self.subTest(credential_release=bad), self.assertRaises(ValueError):
                self.renderer.render("fleet", "r/x@sha256:" + "c" * 64, "agi-20261115T050000Z", bad)

    def test_crd_patch_applies_to_the_pinned_upstream_crd(self):
        import yaml
        upstream = ROOT / "infra/kubernetes/substrate-scale/upstream/ate.dev_workerpools.v0.0.9.yaml"
        import hashlib
        self.assertEqual(hashlib.sha256(upstream.read_bytes()).hexdigest(),
                         "8c22dda9e5e12989a176e8facf460e647aaa013f657665e0bb095fc350f48626")
        crd = yaml.safe_load(upstream.read_text())
        scale = crd["spec"]["versions"][0]["subresources"]["scale"]
        self.assertNotIn("labelSelectorPath", scale, "the problem: 0.0.9 exposes no selector")
        patched = copy.deepcopy(crd)
        for op in json.loads((ROOT / "infra/kubernetes/substrate-scale/workerpool-selector.patch.json").read_text()):
            parts = op["path"].split("/")[1:]
            target = patched
            for part in parts[:-1]:
                target = target[int(part)] if isinstance(target, list) else target[part]
            key = int(parts[-1]) if isinstance(target, list) else parts[-1]
            if op["op"] == "test":
                self.assertEqual(target[key], op["value"])
            else:
                self.assertNotIn(key, target)
                target[key] = op["value"]
        version = patched["spec"]["versions"][0]
        self.assertEqual(version["subresources"]["scale"]["labelSelectorPath"], ".spec.scaleSelector")
        self.assertEqual(version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["scaleSelector"]["type"], "string")
        # Nothing else changes.
        version["subresources"]["scale"].pop("labelSelectorPath")
        version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"].pop("scaleSelector")
        self.assertEqual(patched, crd)

    def test_crd_patch_script_refuses_gke_without_its_own_approval(self):
        script = ROOT / "infra/kubernetes/substrate-scale/crd_patch.sh"
        result = subprocess.run(["bash", str(script), "apply", "gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev"],
                                capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "CRD_PATCH_APPROVED": "yes"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CRD_PATCH_GKE_APPROVED", result.stderr)


class SubstrateFleetTests(unittest.TestCase):
    AGENTS = ("feature-rag-specialist", "drift-specialist")

    @staticmethod
    def status(*assignments, error=""):
        return {"data": {"enabled": True, "ateApiError": error, "workerPools": [{"name": "business-agents", "replicas": 2}],
                         "actorTemplates": [{"name": "drift-specialist-1f", "phase": "Ready"},
                                            {"name": "feature-rag-specialist", "phase": "Ready"}, {"name": "other-9", "phase": "Ready"}],
                         "actors": [{"actorId": f"a{i}", "status": "Running", "actorTemplateName": template, "ateomPodName": pod,
                                     "workerPoolName": "business-agents"} for i, (template, pod) in enumerate(assignments)],
                         "workers": []}}

    def test_actor_templates_map_to_exactly_one_agent(self):
        from ops.experiments.substrate_fleet import agent_for
        self.assertEqual(agent_for("drift-specialist-1a2b", self.AGENTS), "drift-specialist")
        self.assertEqual(agent_for("feature-rag-specialist", self.AGENTS), "feature-rag-specialist")
        for name in ("drift-specialist-v2", "drift-specialists", "other-1a"):
            with self.subTest(name):
                self.assertIsNone(agent_for(name, self.AGENTS))

    def test_sharing_needs_one_pod_to_host_both_agents_over_the_run(self):
        from ops.experiments.substrate_fleet import snapshot, summarize
        pod_a, pod_b = "business-agents-deployment-x-1", "business-agents-deployment-x-2"
        separate = [snapshot(self.status(("drift-specialist-1f", pod_a), ("feature-rag-specialist", pod_b)), self.AGENTS)]
        self.assertEqual(summarize(separate, self.AGENTS, "business-agents")["verdict"], "NOT_OBSERVED")
        later = separate + [snapshot(self.status(("feature-rag-specialist", pod_a)), self.AGENTS)]
        result = summarize(later, self.AGENTS, "business-agents")
        self.assertEqual((result["verdict"], result["shared_pods"]), ("SHARED_WORKERS_OBSERVED", [pod_a]))
        failed = [{"collect_error": "URLError"}, snapshot(self.status(error="ate-api down"), self.AGENTS)]
        self.assertEqual(summarize(failed, self.AGENTS, "business-agents")["verdict"], "NO_DATA")


if __name__ == "__main__":
    unittest.main()
