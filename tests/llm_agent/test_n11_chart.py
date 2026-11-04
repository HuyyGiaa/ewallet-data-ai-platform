"""N11 chart contracts: NL intake env, BYO audit logs, gateway tracing, KFP UI policy.

Every N11 switch is opt-in, so a release rendered with its existing values is
byte-identical; each switch changes only the objects its approval packet names.
"""

from __future__ import annotations

import unittest

import yaml

from tests.llm_agent.test_agents_chart import CHART, HELM, ON, SCHEMAS, by_kind, render, schema_errors


IMAGE = "asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/g3-byo-coordinator@sha256:" + "e" * 64
SPECIALIST = IMAGE.replace("g3-byo-coordinator", "g3-byo-feature-rag-specialist").replace("e" * 64, "b" * 64)
BYO = {
    "agents": {
        "mode": "agent",
        "coordinator": {"byo": {"enabled": True, "image": IMAGE}},
        "specialists": {"feature-rag-specialist": {"byo": {"enabled": True, "image": SPECIALIST}}},
    },
    "otelCollector": {"enabled": True, "tracesEndpoint": "tempo.ewallet-agents.svc.cluster.local:4317"},
    "mcpCredentialLifecycle": {"clientRelease": "r1-release1"},
}
NL = {"agents": {"coordinator": {"byo": {"naturalLanguageIntake": {"enabled": True}}}}}
AUDIT = {"agents": {"byoTelemetry": {"auditLogs": {"enabled": True}}}}


def env(agent):
    return {item["name"]: item for item in agent["spec"]["byo"]["deployment"]["env"]}


def keyed(items):
    return {(item["kind"], item["metadata"].get("namespace"), item["metadata"]["name"]): item for item in items}


@unittest.skipUnless(HELM, "helm binary unavailable")
class NaturalLanguageIntakeChartTests(unittest.TestCase):
    def test_disabled_by_default_and_enabling_changes_only_the_coordinator(self):
        before, after = keyed(render(ON, BYO)), keyed(render(ON, BYO, NL))
        self.assertNotIn("EWALLET_NL_INTAKE_ENABLED", env(before[("Agent", "ewallet-agents", "fraud-coordinator")]))
        self.assertEqual([key for key in before if before[key] != after[key]],
                         [("Agent", "ewallet-agents", "fraud-coordinator")])

    def test_coordinator_uses_the_modelconfig_route_and_client_key_secret_reference(self):
        agents = by_kind(render(ON, BYO, NL), "Agent")
        values = env(agents["fraud-coordinator"])
        self.assertEqual(values["EWALLET_LLM_BASE_URL"]["value"],
                         "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1")
        self.assertEqual(values["EWALLET_LLM_MODEL"]["value"], "ewallet-fraud-llm")
        self.assertEqual(values["EWALLET_LLM_API_KEY"]["valueFrom"]["secretKeyRef"],
                         {"name": "agentgateway-client-key", "key": "api-key"})
        self.assertNotIn("value", values["EWALLET_LLM_API_KEY"])
        self.assertNotIn("EWALLET_LLM_API_KEY", env(agents["feature-rag-specialist"]))
        self.assertEqual(schema_errors(agents["fraud-coordinator"]["spec"], SCHEMAS["Agent"]["schema"]["properties"]["spec"]), [])

    def test_specialist_opt_in_gets_the_same_route_and_secret_reference(self):
        spec_nl = {"agents": {"specialists": {"feature-rag-specialist": {"byo": {"naturalLanguageIntake": {"enabled": True}}}}}}
        before, after = keyed(render(ON, BYO)), keyed(render(ON, BYO, spec_nl))
        self.assertEqual([key for key in before if before[key] != after[key]],
                         [("Agent", "ewallet-agents", "feature-rag-specialist")])
        values = env(after[("Agent", "ewallet-agents", "feature-rag-specialist")])
        self.assertEqual(values["EWALLET_LLM_API_KEY"]["valueFrom"]["secretKeyRef"],
                         {"name": "agentgateway-client-key", "key": "api-key"})
        self.assertEqual(values["EWALLET_MCP_SERVICE_TOKEN"]["valueFrom"]["secretKeyRef"]["key"], "token")

    def test_llm_timeout_must_nest_above_the_gateway_budget(self):
        for seconds in (5, 250):
            with self.subTest(seconds=seconds):
                bad = {"agents": {"coordinator": {"byo": {"naturalLanguageIntake": {"enabled": True, "timeoutSeconds": seconds}}}},
                       "agentgateway": {"requestTimeout": "30s"}}
                self.assertNotEqual(render(ON, BYO, bad, check=False).returncode, 0)


@unittest.skipUnless(HELM, "helm binary unavailable")
class AuditLogChartTests(unittest.TestCase):
    def test_audit_logs_change_only_the_two_byo_agents(self):
        before, after = keyed(render(ON, BYO)), keyed(render(ON, BYO, AUDIT))
        changed = sorted(key for key in before if before[key] != after[key])
        self.assertEqual(changed, [("Agent", "ewallet-agents", "feature-rag-specialist"),
                                   ("Agent", "ewallet-agents", "fraud-coordinator")])
        for name in ("fraud-coordinator", "feature-rag-specialist"):
            values = env(after[("Agent", "ewallet-agents", name)])
            self.assertEqual(values["EWALLET_OTEL_LOGS_ENABLED"]["value"], "true")
            self.assertEqual(values["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"]["value"],
                             "otel-collector.ewallet-agents.svc.cluster.local:4317")
            self.assertEqual(values["OTEL_LOGGING_ENABLED"]["value"], "false")

    def test_audit_logs_require_the_shared_collector(self):
        no_collector = {"otelCollector": {"enabled": False}}
        self.assertNotEqual(render(ON, BYO, AUDIT, no_collector, check=False).returncode, 0)


@unittest.skipUnless(HELM, "helm binary unavailable")
class GatewayTracingChartTests(unittest.TestCase):
    def test_tracing_block_is_added_to_all_three_gateways_only_when_enabled(self):
        policy = {"mcpGateway": {"enabled": True}, "a2aGateway": {"enabled": True}}
        before = by_kind(render(ON, BYO, policy), "ConfigMap")
        after = by_kind(render(ON, BYO, policy, {"gatewayTracing": {"enabled": True}}), "ConfigMap")
        for name in ("agentgateway-llm-config", "mcp-gateway-config", "a2a-gateway-config"):
            with self.subTest(name=name):
                key = next(iter(after[name]["data"]))
                config = yaml.safe_load(after[name]["data"][key])
                self.assertEqual(config["config"]["tracing"], {
                    "otlpEndpoint": "http://otel-collector.ewallet-agents.svc.cluster.local:4317",
                    "otlpProtocol": "grpc",
                    "randomSampling": "true",
                })
                self.assertNotIn("tracing", yaml.safe_load(before[name]["data"][key]).get("config") or {})

    def test_observability_checksum_is_not_touched(self):
        policy = {"mcpGateway": {"enabled": True}, "a2aGateway": {"enabled": True},
                  "observabilityStack": {"enabled": True}}
        before = by_kind(render(ON, BYO, policy), "Deployment")
        after = by_kind(render(ON, BYO, policy, {"gatewayTracing": {"enabled": True}}), "Deployment")
        for name in ("prometheus", "grafana", "otel-collector", "vllm-primary"):
            if name in before:
                self.assertEqual(before[name], after[name], name)


@unittest.skipUnless(HELM, "helm binary unavailable")
class KfpUiPolicyChartTests(unittest.TestCase):
    ML = {"observabilityStack": {"enabled": True, "mlPlatform": {"enabled": True}}}
    ADOPT = {"networkPolicy": {"kfpUiIngress": {"enabled": True}}}

    def test_adoption_switch_adds_exactly_the_hotfix_policy(self):
        before, after = keyed(render(ON, BYO, self.ML)), keyed(render(ON, BYO, self.ML, self.ADOPT))
        self.assertEqual(set(before) - set(after), set())
        added = set(after) - set(before)
        self.assertEqual(added, {("NetworkPolicy", "kubeflow", "allow-kfp-ui-to-ml-pipeline")})
        self.assertEqual([key for key in before if before[key] != after[key]], [])
        policy = after[("NetworkPolicy", "kubeflow", "allow-kfp-ui-to-ml-pipeline")]
        self.assertEqual(policy["spec"], {
            "podSelector": {"matchLabels": {"app": "ml-pipeline"}},
            "policyTypes": ["Ingress"],
            "ingress": [{
                "from": [{"podSelector": {"matchLabels": {"app": "ml-pipeline-ui",
                                                          "application-crd-id": "kubeflow-pipelines"}}}],
                "ports": [{"protocol": "TCP", "port": 8888}],
            }],
        })
        # Same-namespace pod selector only: no namespaceSelector, no ipBlock.
        peer = policy["spec"]["ingress"][0]["from"][0]
        self.assertEqual(set(peer), {"podSelector"})

    def test_policy_is_not_rendered_without_the_ml_platform_flow(self):
        items = keyed(render(ON, BYO, self.ADOPT))
        self.assertNotIn(("NetworkPolicy", "kubeflow", "allow-kfp-ui-to-ml-pipeline"), items)

    def test_policy_matches_the_live_n10e2_hotfix_manifest(self):
        hotfix = yaml.safe_load(
            (CHART / "n11" / "kfp-ui-policy-hotfix-n10e2.yaml").read_text(encoding="utf-8")
        )
        rendered = keyed(render(ON, BYO, self.ML, self.ADOPT))[("NetworkPolicy", "kubeflow", "allow-kfp-ui-to-ml-pipeline")]
        self.assertEqual(rendered["spec"], hotfix["spec"])
        self.assertEqual(rendered["metadata"]["name"], hotfix["metadata"]["name"])


if __name__ == "__main__":
    unittest.main()
