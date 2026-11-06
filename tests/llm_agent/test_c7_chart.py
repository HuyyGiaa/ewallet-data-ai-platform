"""C7 chart contracts: experiments are inert by default and change only what they name."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

import yaml

from tests.llm_agent.test_agents_chart import CHART, ON, by_kind, render
from tests.llm_agent.test_n11_chart import BYO, NL, env, keyed

C7 = Path(CHART) / "c7"
DIGEST = "asia-southeast1-docker.pkg.dev/p/r/vllm-gpu-model@sha256:" + "c" * 64
CANDIDATE_IMAGE = {"vllmCandidate": {"image": DIGEST}}


def overlay(name):
    return yaml.safe_load((C7 / name).read_text())


def gateway_config(items):
    return yaml.safe_load(by_kind(items, "ConfigMap")["agentgateway-llm-config"]["data"]["config.yaml"])


def models(items):
    return {model["name"]: model for model in gateway_config(items)["llm"]["models"]}


def virtual(items):
    return {model["name"]: model for model in gateway_config(items)["llm"]["virtualModels"]}


class ExperimentDefaultsTests(unittest.TestCase):
    def test_nothing_renders_without_an_overlay(self):
        items = render(ON, BYO, NL)
        self.assertNotIn("vllm-candidate", by_kind(items, "Deployment"))
        self.assertNotIn("EWALLET_AGENT_AB_CONFIG", env(by_kind(items, "Agent")["fraud-coordinator"]))
        self.assertEqual({"ewallet-fraud-llm"}, set(virtual(items)))

    def test_agent_overlay_changes_only_the_gateway_config_and_the_coordinator(self):
        before, after = keyed(render(ON, BYO, NL)), keyed(render(ON, BYO, NL, overlay("agent-ab-cpu.yaml")))
        self.assertEqual(set(before), set(after))
        changed = {key for key in before if before[key] != after[key]}
        self.assertEqual(
            {("Agent", "ewallet-agents", "fraud-coordinator"),
             ("ConfigMap", "ewallet-agents", "agentgateway-llm-config")},
            changed,
        )


class AgentVariantTests(unittest.TestCase):
    def setUp(self):
        self.items = render(ON, BYO, NL, overlay("agent-ab-cpu.yaml"))
        self.config = json.loads(env(by_kind(self.items, "Agent")["fraud-coordinator"])["EWALLET_AGENT_AB_CONFIG"]["value"])

    def test_candidate_model_name_is_a_real_gateway_route(self):
        requested = {variant["model"] for variant in self.config["variants"]}
        self.assertLessEqual(requested, set(virtual(self.items)))

    def test_candidate_route_is_attributable(self):
        route = models(self.items)["ewallet-agent-candidate-route"]
        self.assertEqual("agent-candidate", route["responseHeaders"]["set"]["x-ewallet-backend"])

    def test_variants_differ_only_in_model_configuration(self):
        self.assertEqual({"name", "weight", "model", "temperature", "max_tokens"}, set(self.config["variants"][0]))
        self.assertEqual([50, 50], [variant["weight"] for variant in self.config["variants"]])

    def test_experiment_without_routes_is_refused(self):
        spec = overlay("agent-ab-cpu.yaml")
        spec["modelExperiments"]["enabled"] = False
        with self.assertRaises(Exception):
            render(ON, BYO, NL, spec)


class EvaluationConfigTests(unittest.TestCase):
    def test_eval_configs_are_the_ones_the_overlays_deploy(self):
        root = Path(CHART).parents[2] / "configs/experiments/agent-ab"
        for name, source in (("sampling", "agent-ab-cpu.yaml"), ("model", "agent-ab-qwen-1-7b.yaml")):
            deployed = overlay(source)["agents"]["coordinator"]["byo"]["abExperiment"]
            config = json.loads((root / f"{name}.json").read_text())
            self.assertEqual(deployed["variants"], config["variants"])
            self.assertEqual(deployed["experimentId"], config["experiment_id"])


class LlmAbTests(unittest.TestCase):
    def setUp(self):
        self.items = render(ON, BYO, NL, overlay("llm-ab-cpu-gpu.yaml"), CANDIDATE_IMAGE)

    def test_weighted_route_points_at_both_models(self):
        targets = virtual(self.items)["ewallet-llm-ab"]["routing"]["weighted"]["targets"]
        self.assertEqual([("ewallet-fraud-llm-primary", 90), ("ewallet-llm-qwen3-1-7b", 10)],
                         [(target["model"], target["weight"]) for target in targets])

    def test_production_failover_route_is_unchanged(self):
        self.assertEqual(virtual(render(ON, BYO, NL))["ewallet-fraud-llm"], virtual(self.items)["ewallet-fraud-llm"])

    def test_gpu_candidate_is_isolated_and_requests_one_gpu(self):
        spec = by_kind(self.items, "Deployment")["vllm-candidate"]["spec"]["template"]["spec"]
        self.assertEqual({"cloud.google.com/gke-accelerator": "nvidia-l4"}, spec["nodeSelector"])
        self.assertEqual(1, spec["containers"][0]["resources"]["limits"]["nvidia.com/gpu"])
        self.assertIn({"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}, spec["tolerations"])
        self.assertIn("--gpu-memory-utilization", spec["containers"][0]["args"])

    def test_candidate_carries_the_label_the_gateway_network_policy_selects(self):
        labels = by_kind(self.items, "Deployment")["vllm-candidate"]["spec"]["template"]["metadata"]["labels"]
        self.assertEqual("vllm", labels["app.kubernetes.io/name"])

    def test_stacked_agent_overlay_keeps_the_llm_route(self):
        items = render(ON, BYO, NL, overlay("llm-ab-cpu-gpu.yaml"), CANDIDATE_IMAGE, overlay("agent-ab-qwen-1-7b.yaml"))
        self.assertLessEqual({"ewallet-llm-ab", "ewallet-agent-candidate"}, set(virtual(items)))

    def test_unpinned_image_is_refused(self):
        with self.assertRaises(Exception):
            render(ON, BYO, NL, overlay("llm-ab-cpu-gpu.yaml"), {"vllmCandidate": {"image": "vllm/vllm-openai:latest"}})

    def test_weights_must_total_100(self):
        spec = overlay("llm-ab-cpu-gpu.yaml")
        spec["modelExperiments"]["virtualModels"][0]["targets"][1]["weight"] = 20
        with self.assertRaises(Exception):
            render(ON, BYO, NL, spec, CANDIDATE_IMAGE)
