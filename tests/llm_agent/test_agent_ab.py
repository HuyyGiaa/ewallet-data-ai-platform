"""Agent variant assignment, effective model configuration and attribution."""

from __future__ import annotations

import asyncio
from collections import Counter
import json
import os
import unittest
from unittest.mock import patch

import httpx

from ai_platform.llm_agent.agent_ab import AgentExperiment, AgentExperimentError, ExperimentPlanner
from ai_platform.llm_agent.agent_metrics import ab_outcome, record_agent_ab
from ai_platform.llm_agent.nl_intake import IntakeFailure, OpenAIToolPlanner, plan, planner_from_env
from tests.llm_agent.test_n11_nl_intake import KEY, PROMPT, completion

CONFIG = {
    "experiment_id": "agent-ab", "salt": "s",
    "variants": [
        {"name": "baseline", "weight": 50, "model": "ewallet-fraud-llm", "temperature": 0, "max_tokens": 384},
        {"name": "candidate", "weight": 50, "model": "ewallet-agent-candidate", "temperature": 0.3, "max_tokens": 256},
    ],
}


class Gateway:
    """Answer like agentgateway: echo the model and name the backend."""

    def __init__(self, body=None):
        self.body = body or completion()
        self.requests = []

    def handler(self, request):
        sent = json.loads(request.content)
        self.requests.append(sent)
        backend = "agent-candidate" if sent["model"] == "ewallet-agent-candidate" else "primary"
        return httpx.Response(200, json={**self.body, "model": f"served-{sent['model']}"},
                              headers={"x-ewallet-backend": backend})

    def planners(self, experiment):
        return {
            variant.name: OpenAIToolPlanner(
                model=variant.model, temperature=variant.temperature, max_tokens=variant.max_tokens,
                variant=variant.name, key_reader=lambda: KEY, transport=httpx.MockTransport(self.handler),
            )
            for variant in experiment.variants
        }


def experiment(**changes):
    return AgentExperiment.from_json(json.dumps({**CONFIG, **changes}))


class ConfigurationTests(unittest.TestCase):
    def test_identical_variants_are_refused(self):
        same = [dict(CONFIG["variants"][0]), {**CONFIG["variants"][0], "name": "candidate"}]
        with self.assertRaises(AgentExperimentError):
            experiment(variants=same)

    def test_wrong_order_or_names_are_refused(self):
        with self.assertRaises(AgentExperimentError):
            experiment(variants=list(reversed(CONFIG["variants"])))

    def test_out_of_range_sampling_is_refused(self):
        bad = [CONFIG["variants"][0], {**CONFIG["variants"][1], "temperature": 3}]
        with self.assertRaises(AgentExperimentError):
            experiment(variants=bad)

    def test_malformed_json_is_refused(self):
        with self.assertRaises(AgentExperimentError):
            AgentExperiment.from_json("{")

    def test_environment_builds_two_planners_on_the_approved_route(self):
        values = {"EWALLET_NL_INTAKE_ENABLED": "true", "EWALLET_AGENT_AB_CONFIG": json.dumps(CONFIG)}
        with patch.dict(os.environ, values):
            self.assertIsInstance(planner_from_env(), ExperimentPlanner)

    def test_without_the_variable_the_single_planner_stays(self):
        with patch.dict(os.environ, {"EWALLET_NL_INTAKE_ENABLED": "true"}):
            os.environ.pop("EWALLET_AGENT_AB_CONFIG", None)
            planner = planner_from_env()
        self.assertIsInstance(planner, OpenAIToolPlanner)
        self.assertEqual("", planner.variant)


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.experiment = experiment()
        self.gateway = Gateway()
        self.planner = ExperimentPlanner(self.experiment, self.gateway.planners(self.experiment))

    def test_a_conversation_keeps_one_variant(self):
        self.assertEqual(1, len({id(self.planner.for_context("ctx-7")) for _ in range(20)}))

    def test_conversations_split_near_the_configured_weights(self):
        counts = Counter(self.planner.for_context(f"ctx-{i}").variant for i in range(4_000))
        self.assertLess(abs(counts["candidate"] / 4_000 - 0.5), 0.03)

    def test_missing_context_uses_baseline(self):
        self.assertEqual("baseline", self.planner.for_context("").variant)

    def test_rollback_weights_send_every_conversation_to_baseline(self):
        rollback = experiment(variants=[{**CONFIG["variants"][0], "weight": 100}, {**CONFIG["variants"][1], "weight": 0}])
        planner = ExperimentPlanner(rollback, self.gateway.planners(rollback))
        self.assertEqual({"baseline"}, {planner.for_context(f"c{i}").variant for i in range(2_000)})

    def test_variants_change_model_settings_but_not_prompt_or_tools(self):
        for context in ("a", "b", "c", "d", "e", "f", "g", "h"):
            asyncio.run(plan(PROMPT, planner=self.planner.for_context(context), context_id=context, message_id="m"))
        by_model = {request["model"]: request for request in self.gateway.requests}
        self.assertEqual({"ewallet-fraud-llm", "ewallet-agent-candidate"}, set(by_model))
        baseline, candidate = by_model["ewallet-fraud-llm"], by_model["ewallet-agent-candidate"]
        self.assertEqual((0, 384), (baseline["temperature"], baseline["max_tokens"]))
        self.assertEqual((0.3, 256), (candidate["temperature"], candidate["max_tokens"]))
        self.assertEqual(baseline["tools"], candidate["tools"])
        self.assertEqual(baseline["messages"], candidate["messages"])

    def test_selection_reports_what_the_gateway_really_served(self):
        context = next(f"c{i}" for i in range(100) if self.planner.for_context(f"c{i}").variant == "candidate")
        result = asyncio.run(plan(PROMPT, planner=self.planner.for_context(context), context_id=context, message_id="m"))
        self.assertEqual("candidate", result.selection.variant)
        self.assertEqual("served-ewallet-agent-candidate", result.selection.served_model)
        self.assertEqual("agent-candidate", result.selection.backend)

    def test_grounding_still_rejects_invented_arguments_on_the_candidate(self):
        invented = completion({**json.loads(completion()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]),
                               "user_id": "00000000-0000-4000-8000-000000000000"})
        gateway = Gateway(invented)
        planner = ExperimentPlanner(self.experiment, gateway.planners(self.experiment))
        context = next(f"c{i}" for i in range(100) if planner.for_context(f"c{i}").variant == "candidate")
        with self.assertRaises(IntakeFailure) as caught:
            asyncio.run(plan(PROMPT, planner=planner.for_context(context), context_id=context, message_id="m"))
        self.assertEqual("ungrounded_argument", caught.exception.code)


class MetricTests(unittest.TestCase):
    def test_every_failure_maps_to_a_bounded_outcome(self):
        self.assertEqual("selected", ab_outcome(None))
        self.assertEqual("no_tool", ab_outcome("unsupported_request"))
        self.assertEqual("invalid_tool", ab_outcome("unauthorized_tool"))
        self.assertEqual("invalid_arguments", ab_outcome("ungrounded_argument"))
        self.assertEqual("llm_error", ab_outcome("llm_timeout"))

    def test_unknown_variant_is_refused(self):
        with self.assertRaises(ValueError):
            record_agent_ab("other", "selected", latency_seconds=0.1)


if __name__ == "__main__":
    unittest.main()
