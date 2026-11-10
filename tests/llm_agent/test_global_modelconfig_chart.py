"""Chart contract for modelConfig.byoResolver: BYO agents read the global ModelConfig CR.

Off (default) the render is unchanged. On, only the BYO agents with natural-language
intake change, plus one Role (get on one name), one RoleBinding (those ServiceAccounts)
and one egress NetworkPolicy (API server endpoints only).
"""

from __future__ import annotations

import unittest

from tests.llm_agent.test_agents_chart import HELM, ON, SCHEMAS, by_kind, render, schema_errors
from tests.llm_agent.test_n11_chart import BYO, NL, env, keyed


SPEC_NL = {"agents": {"specialists": {"feature-rag-specialist": {"byo": {"naturalLanguageIntake": {"enabled": True}}}}}}
RESOLVER = {"modelConfig": {"byoResolver": {"enabled": True, "apiServer": {"cidrs": ["34.118.224.1/32", "10.20.0.8/32"]}}}}
NS = "ewallet-agents"


@unittest.skipUnless(HELM, "helm binary unavailable")
class ByoResolverChartTests(unittest.TestCase):
    def test_off_by_default_renders_the_legacy_bridge(self):
        agents = by_kind(render(ON, BYO, NL), "Agent")
        values = env(agents["fraud-coordinator"])
        self.assertIn("EWALLET_LLM_MODEL", values)
        self.assertNotIn("EWALLET_LLM_CONFIG_SOURCE", values)

    def test_on_changes_only_llm_byo_agents_and_adds_three_least_privilege_objects(self):
        before, after = keyed(render(ON, BYO, NL, SPEC_NL)), keyed(render(ON, BYO, NL, SPEC_NL, RESOLVER))
        self.assertEqual(sorted(k for k in before if before[k] != after[k]),
                         [("Agent", NS, "feature-rag-specialist"), ("Agent", NS, "fraud-coordinator")])
        self.assertEqual(sorted(set(after) - set(before)), [
            ("NetworkPolicy", NS, "allow-byo-modelconfig-to-kube-apiserver"),
            ("Role", NS, "ewallet-global-llm-reader"), ("RoleBinding", NS, "ewallet-global-llm-reader")])
        self.assertEqual(set(before) - set(after), set())

    def test_byo_env_names_the_cr_and_only_the_secret_reference(self):
        agents = by_kind(render(ON, BYO, NL, SPEC_NL, RESOLVER), "Agent")
        for name in ("fraud-coordinator", "feature-rag-specialist"):
            values = env(agents[name])
            self.assertEqual(values["EWALLET_LLM_CONFIG_SOURCE"]["value"], "modelconfig")
            self.assertEqual(values["EWALLET_LLM_MODELCONFIG_NAME"]["value"], "ewallet-global-llm")
            self.assertEqual(values["EWALLET_LLM_API_KEY_SECRET_REF"]["value"], "agentgateway-client-key/api-key")
            self.assertEqual(values["EWALLET_LLM_API_KEY"]["valueFrom"]["secretKeyRef"],
                             {"name": "agentgateway-client-key", "key": "api-key"})
            self.assertNotIn("EWALLET_LLM_BASE_URL", values)
            self.assertNotIn("EWALLET_LLM_MODEL", values)
            dep = agents[name]["spec"]["byo"]["deployment"]
            (volume,) = dep["volumes"]
            sources = volume["projected"]["sources"]
            self.assertEqual(sources[0]["serviceAccountToken"], {"path": "token", "expirationSeconds": 3600})
            self.assertNotIn("audience", sources[0]["serviceAccountToken"])     # API server audience
            self.assertEqual(sources[1]["configMap"]["name"], "kube-root-ca.crt")
            self.assertEqual(dep["volumeMounts"], [{"name": "kube-api-modelconfig", "readOnly": True,
                                                    "mountPath": "/var/run/secrets/ewallet/kube-api"}])
            self.assertEqual(schema_errors(agents[name]["spec"], SCHEMAS["Agent"]["schema"]["properties"]["spec"]), [])

    def test_declarative_drift_keeps_its_native_reference(self):
        agents = by_kind(render(ON, BYO, NL, RESOLVER), "Agent")
        self.assertEqual(agents["drift-specialist"]["spec"]["declarative"]["modelConfig"], "ewallet-global-llm")
        self.assertNotIn("byo", agents["drift-specialist"]["spec"])

    def test_role_is_get_on_one_name_and_bound_only_to_llm_byo_agents(self):
        items = render(ON, BYO, NL, RESOLVER)          # specialist intake off: not a subject
        role = by_kind(items, "Role")["ewallet-global-llm-reader"]
        self.assertEqual(role["rules"], [{"apiGroups": ["kagent.dev"], "resources": ["modelconfigs"],
                                          "resourceNames": ["ewallet-global-llm"], "verbs": ["get"]}])
        binding = by_kind(items, "RoleBinding")["ewallet-global-llm-reader"]
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "fraud-coordinator", "namespace": NS}])
        policy = by_kind(items, "NetworkPolicy")["allow-byo-modelconfig-to-kube-apiserver"]
        self.assertEqual(policy["spec"]["policyTypes"], ["Egress"])
        self.assertEqual(policy["spec"]["podSelector"]["matchExpressions"][0]["values"], ["fraud-coordinator"])
        self.assertEqual(policy["spec"]["egress"], [{
            "to": [{"ipBlock": {"cidr": "34.118.224.1/32"}}, {"ipBlock": {"cidr": "10.20.0.8/32"}}],
            "ports": [{"protocol": "TCP", "port": 443}]}])
        agents = by_kind(items, "Agent")
        self.assertNotIn("EWALLET_LLM_CONFIG_SOURCE", env(agents["feature-rag-specialist"]))

    def test_guards(self):
        no_cidrs = {"modelConfig": {"byoResolver": {"enabled": True}}}
        proc = render(ON, BYO, NL, no_cidrs, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("byoResolver.apiServer.cidrs is required", proc.stderr)
        proc = render(ON, BYO, RESOLVER, check=False)             # no BYO agent calls an LLM
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("needs at least one BYO agent", proc.stderr)


if __name__ == "__main__":
    unittest.main()
