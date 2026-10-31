"""R2 (llm-d) and R3 (kagent A2A + MCP) timeout budgets nest per environment.

Same rule as R1: every caller waits longer than the hop below it, so the
inner hop answers first and no hop keeps working on an abandoned request.
Pairs checked: gcp = base values; local = values-local + the *-local overlays;
gke-cpu = values-gke-cpu + the same CPU (*-local) router and kagent overlays.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
LLMD = ROOT / "infra/helm/llm-d"
KAGENT = ROOT / "infra/kubernetes/kagent-platform"
HELM = shutil.which("helm")


def merge(base, override):
    out = dict(base)
    for key, value in override.items():
        out[key] = merge(out.get(key, {}), value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load(*paths):
    values = {}
    for path in paths:
        values = merge(values, yaml.safe_load(Path(path).read_text()) or {})
    return values


def seconds(value):
    value = str(value)
    if value.endswith("ms"):
        return float(value[:-2]) / 1000
    return float(value[:-1]) * (60 if value.endswith("m") else 1) if value[-1] in "sm" else float(value)


ENVS = {
    "gcp": dict(chart=[CHART / "values.yaml", CHART / "values-gcp.yaml"],
                router=[LLMD / "router-values.yaml"], kagent=[KAGENT / "kagent-values.yaml"]),
    "local": dict(chart=[CHART / "values.yaml", CHART / "values-local.yaml"],
                  router=[LLMD / "router-values.yaml", LLMD / "router-values-local.yaml"],
                  kagent=[KAGENT / "kagent-values.yaml", KAGENT / "kagent-values-local.yaml"]),
    "gke-cpu": dict(chart=[CHART / "values.yaml", CHART / "values-gke-cpu.yaml"],
                    router=[LLMD / "router-values.yaml", LLMD / "router-values-local.yaml"],
                    kagent=[KAGENT / "kagent-values.yaml", KAGENT / "kagent-values-local.yaml"]),
}


class BudgetNestingTests(unittest.TestCase):
    def test_llmd_route_answers_before_the_llm_listener_gives_up(self):
        for env, files in ENVS.items():
            with self.subTest(env):
                chart, router = load(*files["chart"]), load(*files["router"])
                route = seconds(router["httpRoute"]["requestTimeout"])
                backend = seconds(chart["agentgateway"]["backendRequestTimeout"])
                self.assertLess(route, backend)
                # ...but leaves most of the generation budget to the routed path.
                self.assertGreaterEqual(route, 0.7 * backend)

    def test_a2a_delegation_budget_nests_and_covers_two_llm_turns(self):
        for env, files in ENVS.items():
            with self.subTest(env):
                chart, kagent = load(*files["chart"]), load(*files["kagent"])
                client = seconds(kagent["controller"]["a2aClientTimeout"])
                gateway = seconds(chart["a2aGateway"]["requestTimeout"])
                turn = float(chart["modelConfig"]["timeoutSeconds"])
                mcp = seconds(chart["agents"]["mcpTimeout"])
                self.assertGreater(client, gateway)
                self.assertGreater(gateway, 2 * turn + mcp)

    def test_mcp_budget_nests_from_agent_to_tool(self):
        for env, files in ENVS.items():
            with self.subTest(env):
                chart = load(*files["chart"])
                self.assertGreater(seconds(chart["agents"]["mcpTimeout"]), seconds(chart["mcpGateway"]["requestTimeout"]))
                self.assertGreater(seconds(chart["mcpGateway"]["requestTimeout"]), chart["mcpServices"]["maxTimeoutMs"] / 1000)

    def test_vendored_router_render_carries_the_base_route_budget(self):
        rendered = [d for d in yaml.safe_load_all((LLMD / "rendered/router-v0.9.0.yaml").read_text()) if d]
        route = next(d for d in rendered if d["kind"] == "HTTPRoute")
        self.assertEqual(route["spec"]["rules"][0]["timeouts"]["request"],
                         load(LLMD / "router-values.yaml")["httpRoute"]["requestTimeout"])


@unittest.skipUnless(HELM, "helm binary unavailable")
class ChartGuardTests(unittest.TestCase):
    def render(self, overlay, override):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "o.yaml"
            path.write_text(yaml.safe_dump(override))
            return subprocess.run([HELM, "template", "x", str(CHART), "-f", str(CHART / overlay), "-f", str(path)],
                                  capture_output=True, text=True, check=False)

    def test_gateways_render_in_every_overlay(self):
        on = {"mcpGateway": {"enabled": True}, "a2aGateway": {"enabled": True}}
        # values-gke-cpu.yaml serves a derived model image that is set at deploy time.
        image = {"vllm": {"image": "asia-southeast1-docker.pkg.dev/p/r/vllm-cpu-model@sha256:" + "a" * 64}}
        for overlay in ("values-gcp.yaml", "values-local.yaml", "values-dev.yaml", "values-gke-cpu.yaml"):
            with self.subTest(overlay):
                override = {**on, **image} if overlay == "values-gke-cpu.yaml" else on
                self.assertEqual(self.render(overlay, override).returncode, 0)

    def test_guards_reject_non_nested_budgets(self):
        cases = {
            "mcp gateway equals the agent budget": {"mcpGateway": {"enabled": True, "requestTimeout": "30s"}},
            "tool cap above the mcp gateway": {"mcpGateway": {"enabled": True}, "mcpServices": {"maxTimeoutMs": 26000}},
            "a2a below two llm turns": {"a2aGateway": {"enabled": True, "requestTimeout": "90s"}},
        }
        for label, override in cases.items():
            with self.subTest(label):
                self.assertNotEqual(self.render("values-gcp.yaml", override).returncode, 0)


if __name__ == "__main__":
    unittest.main()
