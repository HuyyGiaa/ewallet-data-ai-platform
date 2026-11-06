"""KEDA ownership, bounds and MCP failover overlays rendered by the real chart."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

from tests.llm_agent.test_agents_chart import HELM, ON, render
from tests.llm_agent.test_n11_chart import BYO
from tests.llm_agent.test_phase_d_chart import ENABLED as SERVICES_FIXTURE

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "infra/helm/keda/scaledobject-schema-2.21.0.json"
AUTOSCALING = {"agents": {"autoscaling": {"enabled": True}}}
KEDA = {"agents": {"autoscaling": {"enabled": True, "engine": "keda"}}}


def kinds(items, kind):
    return {item["metadata"]["name"]: item for item in items if item["kind"] == kind}


@unittest.skipUnless(HELM, "helm binary unavailable")
class EngineOwnershipTest(unittest.TestCase):
    def test_the_default_stays_the_hpa(self):
        items = render(ON, BYO, AUTOSCALING)
        self.assertTrue(kinds(items, "HorizontalPodAutoscaler"))
        self.assertFalse(kinds(items, "ScaledObject"))

    def test_keda_replaces_the_hpa_instead_of_joining_it(self):
        items = render(ON, BYO, KEDA)
        scaled = kinds(items, "ScaledObject")
        self.assertTrue(scaled)
        self.assertFalse(kinds(items, "HorizontalPodAutoscaler"))

    def test_each_deployment_has_one_scaler(self):
        for overlay in (AUTOSCALING, KEDA):
            items = render(ON, BYO, overlay)
            names = [item["spec"]["scaleTargetRef"]["name"] for item in items
                     if item["kind"] in {"ScaledObject", "HorizontalPodAutoscaler"}]
            self.assertEqual(len(names), len(set(names)))

    def test_disruption_budgets_survive_the_switch(self):
        self.assertEqual(set(kinds(render(ON, BYO, AUTOSCALING), "PodDisruptionBudget")),
                         set(kinds(render(ON, BYO, KEDA), "PodDisruptionBudget")))

    def test_an_unknown_engine_is_refused(self):
        result = render(ON, BYO, {"agents": {"autoscaling": {"enabled": True, "engine": "both"}}}, check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("engine must be hpa or keda", result.stderr)


@unittest.skipUnless(HELM, "helm binary unavailable")
class ScaledObjectTest(unittest.TestCase):
    def setUp(self):
        self.scaled = kinds(render(ON, BYO, KEDA), "ScaledObject")

    def test_replica_bounds_keep_the_multi_replica_floor(self):
        for item in self.scaled.values():
            self.assertEqual(2, item["spec"]["minReplicaCount"])
            self.assertEqual(3, item["spec"]["maxReplicaCount"])

    def test_it_targets_the_deployment_not_a_worker_pool(self):
        for name, item in self.scaled.items():
            self.assertEqual({"apiVersion": "apps/v1", "kind": "Deployment", "name": name},
                             item["spec"]["scaleTargetRef"])

    def test_the_trigger_is_per_agent_and_supports_fallback(self):
        for name, item in self.scaled.items():
            (trigger,) = item["spec"]["triggers"]
            self.assertEqual("prometheus", trigger["type"])
            self.assertEqual("AverageValue", trigger["metricType"])
            self.assertIn(f'agent="{name}"', trigger["metadata"]["query"])
            self.assertIn("or vector(0)", trigger["metadata"]["query"])
            self.assertEqual(2, item["spec"]["fallback"]["replicas"])

    def test_scale_behaviour_matches_the_hpa_it_replaces(self):
        hpa = next(iter(kinds(render(ON, BYO, AUTOSCALING), "HorizontalPodAutoscaler").values()))
        for item in self.scaled.values():
            behavior = item["spec"]["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]
            self.assertEqual(hpa["spec"]["behavior"], behavior)

    def test_each_object_matches_the_pinned_schema(self):
        import jsonschema

        schema = json.loads(SCHEMA.read_text())
        for item in self.scaled.values():
            jsonschema.validate(item, schema)


@unittest.skipUnless(HELM, "helm binary unavailable")
class McpFailoverOverlayTest(unittest.TestCase):
    OVERLAY = yaml.safe_load((ROOT / "infra/helm/llm-agent-platform/g3/mcp-failover.yaml").read_text())
    SERVICES = SERVICES_FIXTURE

    def deployments(self, *overlays):
        return {name: item for name, item in kinds(render(ON, BYO, self.SERVICES, *overlays), "Deployment").items()
                if name.endswith("-mcp")}

    def test_two_spread_replicas_are_rendered(self):
        for item in self.deployments(self.OVERLAY).values():
            self.assertEqual(2, item["spec"]["replicas"])
            spread = item["spec"]["template"]["spec"]["topologySpreadConstraints"]
            self.assertEqual("kubernetes.io/hostname", spread[0]["topologyKey"])

    def test_spreading_stays_off_by_default(self):
        for item in self.deployments().values():
            self.assertNotIn("topologySpreadConstraints", item["spec"]["template"]["spec"])

    def test_a_rollout_never_drops_below_the_replica_count(self):
        for item in self.deployments(self.OVERLAY).values():
            self.assertEqual(0, item["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"])


class KedaInstallScriptTest(unittest.TestCase):
    def test_the_installer_refuses_a_second_operator_and_needs_a_context(self):
        text = (ROOT / "scripts/llm_agent/keda_install.sh").read_text()
        self.assertIn("get crd scaledobjects.keda.sh", text)
        self.assertIn("KUBE_CONTEXT is required", text)
        self.assertIn("KEDA_CHART_TGZ_SHA256", text)


if __name__ == "__main__":
    unittest.main()
