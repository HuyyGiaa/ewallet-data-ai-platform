"""N12: PII prompt screen and bounded agent counters (edai-2 rows 50/51)."""

from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from ai_platform.llm_agent.agent_metrics import record_invocation
from ai_platform.llm_agent.prompt_safety import screen_prompt
from tests.llm_agent.test_g3_programmatic_coordinator import request
from tests.llm_agent.test_n11_a2a_streaming import coordinator_app, stream, terminal_states
from tests.llm_agent.test_n11_nl_intake import PROMPT, RecordingLLM, completion

READER = InMemoryMetricReader()


def setUpModule():
    metrics.set_meter_provider(MeterProvider(metric_readers=[READER]))


def points(name: str) -> dict[tuple, int]:
    data = READER.get_metrics_data()
    found: dict[tuple, int] = {}
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    for point in metric.data.data_points:
                        found[tuple(sorted(point.attributes.items()))] = point.value
    return found


def value(name: str, **labels) -> int:
    return points(name).get(tuple(sorted(labels.items())), 0)


EMAIL, PHONE, CARD = "nguyen.van.a@example.com", "0912 345 678", "4111 1111 1111 1111"


class PromptSafetyTests(unittest.TestCase):
    def test_detects_and_masks_each_type_without_returning_values(self):
        result = screen_prompt(f"email {EMAIL}, sđt {PHONE}, +84912345678, thẻ {CARD}")
        self.assertEqual(result.counts, {"email": 1, "phone": 2, "payment_card": 1})
        for secret in (EMAIL, PHONE, "+84912345678", CARD):
            self.assertNotIn(secret, result.text)
            self.assertNotIn(secret, repr(result))
        self.assertIn("[REDACTED_EMAIL]", result.text)
        self.assertIn("[REDACTED_PAYMENT_CARD]", result.text)

    def test_fraud_intake_fields_are_not_false_positives(self):
        result = screen_prompt(PROMPT + " amount 1000000 VND, ref 1234567890123")
        self.assertFalse(result.detected)
        self.assertEqual(result.text, PROMPT + " amount 1000000 VND, ref 1234567890123")

    def test_non_luhn_digit_runs_and_non_mobile_numbers_are_kept(self):
        for text in ("4111 1111 1111 1112", "0212345678", "số 0112345678"):
            with self.subTest(text=text):
                self.assertFalse(screen_prompt(text).detected)


class MetricsResourceTests(unittest.TestCase):
    def test_each_replica_exports_a_distinct_service_instance_id(self):
        import os
        from unittest.mock import patch
        from ai_platform.llm_agent import byo_runtime
        created = []

        class Capture:
            def __init__(self, *, resource, metric_readers):
                created.append(resource.attributes)

        env = {"EWALLET_OTEL_METRICS_ENABLED": "true", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "collector:4317"}
        for pod in ("fraud-coordinator-7587f8c764-6s4mx", "fraud-coordinator-7587f8c764-d5prr"):
            with patch.dict(os.environ, {**env, "HOSTNAME": pod}, clear=True), \
                    patch.object(byo_runtime, "_METER_PROVIDER", None), \
                    patch("opentelemetry.sdk.metrics.MeterProvider", Capture), \
                    patch("opentelemetry.metrics.set_meter_provider", lambda provider: None):
                self.assertTrue(byo_runtime.configure_agent_metrics("fraud-coordinator", "1.0.0"))
        self.assertEqual([attrs["service.instance.id"] for attrs in created],
                         ["fraud-coordinator-7587f8c764-6s4mx", "fraud-coordinator-7587f8c764-d5prr"])
        self.assertEqual({attrs["service.name"] for attrs in created}, {"fraud-coordinator"})


class AgentCounterTests(unittest.TestCase):
    def test_pii_is_masked_before_the_llm_and_counted(self):
        llm = RecordingLLM()
        app, _ = coordinator_app(planner=llm.planner())
        before_email = value("llm_prompt_pii_detections_total", agent="fraud-coordinator", pii_type="email")
        before_masked = value("llm_prompts_screened_total", agent="fraud-coordinator", result="pii_masked")
        with TestClient(app) as client:
            events = stream(client, PROMPT + f" Liên hệ khách: {EMAIL}, {PHONE}.")
        self.assertEqual(terminal_states(events), ["completed"])
        sent = llm.requests[0].content.decode()
        self.assertNotIn(EMAIL, sent)
        self.assertNotIn("0912 345 678", sent)
        self.assertIn("[REDACTED_EMAIL]", json.loads(sent)["messages"][1]["content"])
        self.assertEqual(value("llm_prompt_pii_detections_total", agent="fraud-coordinator", pii_type="email"),
                         before_email + 1)
        self.assertEqual(value("llm_prompts_screened_total", agent="fraud-coordinator", result="pii_masked"),
                         before_masked + 1)
        self.assertNotIn(EMAIL, json.dumps(events, ensure_ascii=False))

    def test_clean_prompt_is_counted_as_screened_clean(self):
        before = value("llm_prompts_screened_total", agent="fraud-coordinator", result="clean")
        app, _ = coordinator_app(planner=RecordingLLM().planner())
        with TestClient(app) as client:
            stream(client, PROMPT)
        self.assertEqual(value("llm_prompts_screened_total", agent="fraud-coordinator", result="clean"), before + 1)

    def test_each_terminal_outcome_is_counted_once_per_task(self):
        name = "agent_invocations_total"
        cases = [
            ({"planner": None}, request().to_wire_text(), request().request_id, "typed", "completed"),
            ({"planner": RecordingLLM().planner()}, PROMPT, None, "natural-language", "completed"),
            ({"planner": RecordingLLM().planner()}, "Thời tiết?", None, "natural-language", "rejected"),
            ({"planner": RecordingLLM(status=503, body={}).planner()}, PROMPT, None, "natural-language", "failed"),
            ({"planner": RecordingLLM(completion(name="drop")).planner()}, PROMPT, None, "natural-language", "rejected"),
        ]
        for kwargs, text, message_id, mode, outcome in cases:
            with self.subTest(mode=mode, outcome=outcome, text=text[:20]):
                before = value(name, agent="fraud-coordinator", mode=mode, outcome=outcome)
                app, _ = coordinator_app(**kwargs)
                with TestClient(app) as client:
                    stream(client, text, message_id=message_id)
                self.assertEqual(value(name, agent="fraud-coordinator", mode=mode, outcome=outcome), before + 1)

    def test_initialize_exports_every_bounded_series_at_zero(self):
        from ai_platform.llm_agent.agent_metrics import MODES, OUTCOMES, initialize
        initialize("feature-rag-specialist")
        invocations = {key: v for key, v in points("agent_invocations_total").items()
                       if ("agent", "feature-rag-specialist") in key}
        self.assertEqual(len(invocations), len(MODES) * len(OUTCOMES))
        pii = {key for key in points("llm_prompt_pii_detections_total") if ("agent", "feature-rag-specialist") in key}
        self.assertEqual({dict(key)["pii_type"] for key in pii}, {"email", "phone", "payment_card"})
        before = dict(invocations)
        initialize("feature-rag-specialist")  # idempotent: adds 0
        after = {key: v for key, v in points("agent_invocations_total").items() if key in before}
        self.assertEqual(before, after)

    def test_labels_are_bounded(self):
        for args in (("drift-specialist", "typed", "completed"), ("fraud-coordinator", "x", "completed"),
                     ("fraud-coordinator", "typed", "error")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                record_invocation(*args)


if __name__ == "__main__":
    unittest.main()
