"""Agent integration: Coordinator natural-language routing for fraud, drift and combined requests.

The LLM is a recorded OpenAI-compatible response served by ``httpx.MockTransport``
(mock); grounding, the deterministic router and the typed contracts are real.
"""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from ai_platform.llm_agent.business_request import G3BusinessRequest, G3DriftRequest, G3WorkflowRequest
from ai_platform.llm_agent.nl_intake import TOOL_NAME, IntakeFailure, OpenAIToolPlanner, request_id_for
from ai_platform.llm_agent.workflow_intake import (
    DRIFT_TOOL_NAME,
    WORKFLOW_TOOLS,
    extract_drift_arguments,
    plan_workflow,
    render_workflow_summary,
)
from tests.llm_agent.test_n11_nl_intake import CAPTURED_ARGUMENTS, PROMPT

KEY = "test-client-key"
DRIFT_TEXT = (
    "Phân tích drift của account_behavior__account_tx_count_1h so với baseline bl-r5gb-202604.account_tx_count_1h "
    "trong window 2026-05-01T00:00:00Z to 2026-05-31T00:00:00Z."
)
DRIFT_ARGUMENTS = {
    "feature_name": "account_behavior__account_tx_count_1h",
    "baseline_id": "bl-r5gb-202604.account_tx_count_1h",
    "window_start": "2026-05-01T00:00:00Z",
    "window_end": "2026-05-31T00:00:00Z",
}
COMBINED_TEXT = PROMPT + " Đồng thời " + DRIFT_TEXT[0].lower() + DRIFT_TEXT[1:]


def completion(*calls: tuple[str, dict]):
    return {
        "id": "chatcmpl-agi",
        "model": "ewallet-fraud-llm",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": {
            "role": "assistant", "content": None if calls else "Xin chào",
            "tool_calls": [{"id": f"call-{index}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)}}
                           for index, (name, arguments) in enumerate(calls)]}}],
        "usage": {"prompt_tokens": 700, "completion_tokens": 300},
    }


class LLM:
    def __init__(self, body):
        self.body = body
        self.requests: list[httpx.Request] = []

    def planner(self) -> OpenAIToolPlanner:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=self.body)
        return OpenAIToolPlanner(key_reader=lambda: KEY, transport=httpx.MockTransport(handler),
                                 tools=WORKFLOW_TOOLS, max_tool_calls=2)


def plan(text: str, llm: LLM):
    return asyncio.run(plan_workflow(text, planner=llm.planner(), context_id="ctx", message_id="msg"))


class RoutingTests(unittest.TestCase):
    def test_offers_both_tools_in_one_llm_call(self):
        llm = LLM(completion((DRIFT_TOOL_NAME, DRIFT_ARGUMENTS)))
        plan(DRIFT_TEXT, llm)
        body = json.loads(llm.requests[0].content)
        self.assertEqual([tool["function"]["name"] for tool in body["tools"]], [TOOL_NAME, DRIFT_TOOL_NAME])
        self.assertEqual(len(llm.requests), 1)

    def test_fraud_question_builds_the_unchanged_fraud_request(self):
        result = plan(PROMPT, LLM(completion((TOOL_NAME, CAPTURED_ARGUMENTS))))
        self.assertIsInstance(result.request, G3BusinessRequest)
        self.assertEqual(result.request.request_id, request_id_for("ctx", "msg"))
        self.assertEqual(dict(result.sources), {"fraud-evidence": "llm"})

    def test_drift_question_builds_a_drift_request(self):
        result = plan(DRIFT_TEXT, LLM(completion((DRIFT_TOOL_NAME, DRIFT_ARGUMENTS))))
        self.assertIsInstance(result.request, G3DriftRequest)
        self.assertEqual(result.request.baseline_id, "bl-r5gb-202604.account_tx_count_1h")
        self.assertEqual(dict(result.sources), {"drift": "llm"})

    def test_combined_question_builds_a_workflow_with_both_members(self):
        result = plan(COMBINED_TEXT, LLM(completion((TOOL_NAME, CAPTURED_ARGUMENTS), (DRIFT_TOOL_NAME, DRIFT_ARGUMENTS))))
        self.assertIsInstance(result.request, G3WorkflowRequest)
        self.assertEqual(result.request.fraud.transaction.transaction_id, CAPTURED_ARGUMENTS["transaction_id"])
        self.assertEqual(result.request.drift.feature_name, DRIFT_ARGUMENTS["feature_name"])
        self.assertEqual(dict(result.sources), {"fraud-evidence": "llm", "drift": "llm"})

    def test_explicit_drift_is_completed_deterministically_when_the_llm_omits_it(self):
        result = plan(COMBINED_TEXT, LLM(completion((TOOL_NAME, CAPTURED_ARGUMENTS))))
        self.assertIsInstance(result.request, G3WorkflowRequest)
        self.assertEqual(dict(result.sources), {"fraud-evidence": "llm", "drift": "deterministic"})

    def test_transaction_identifiers_without_fraud_selection_are_not_silently_dropped(self):
        with self.assertRaises(IntakeFailure) as caught:
            plan(COMBINED_TEXT, LLM(completion((DRIFT_TOOL_NAME, DRIFT_ARGUMENTS))))
        self.assertEqual(caught.exception.code, "incomplete_tool_selection")

    def test_window_timestamps_cannot_become_the_transaction_timestamp(self):
        stolen = dict(CAPTURED_ARGUMENTS, event_timestamp="2026-05-01T00:00:00Z")
        with self.assertRaises(IntakeFailure) as caught:
            plan(COMBINED_TEXT, LLM(completion((TOOL_NAME, stolen), (DRIFT_TOOL_NAME, DRIFT_ARGUMENTS))))
        self.assertEqual(caught.exception.code, "ungrounded_argument")


class RejectionTests(unittest.TestCase):
    def test_unrelated_question_never_reaches_the_llm(self):
        llm = LLM(completion())
        with self.assertRaises(IntakeFailure) as caught:
            plan("Thời tiết hôm nay thế nào?", llm)
        self.assertEqual((caught.exception.code, llm.requests), ("unsupported_request", []))

    def test_incomplete_drift_question_is_rejected_before_the_llm(self):
        cases = {
            "no baseline": DRIFT_TEXT.replace("so với baseline bl-r5gb-202604.account_tx_count_1h ", ""),
            "no window": DRIFT_TEXT.split(" trong window")[0] + ".",
            "two features": DRIFT_TEXT.replace("trong window", "và user_behavior__user_avg_amount_30d trong window"),
            "unknown feature only": DRIFT_TEXT.replace("account_behavior__account_tx_count_1h", "account_behavior__score"),
        }
        for label, text in cases.items():
            llm = LLM(completion())
            with self.subTest(label), self.assertRaises(IntakeFailure) as caught:
                plan(text, llm)
            self.assertEqual((caught.exception.code, llm.requests), ("missing_or_ambiguous_field", []))

    def test_llm_drift_arguments_must_equal_the_labelled_text(self):
        for field, value in (("baseline_id", "bl-invented-v9"), ("feature_name", "user_behavior__user_tx_count_1h"),
                             ("window_end", "2026-05-30T00:00:00Z")):
            with self.subTest(field), self.assertRaises(IntakeFailure) as caught:
                plan(DRIFT_TEXT, LLM(completion((DRIFT_TOOL_NAME, dict(DRIFT_ARGUMENTS, **{field: value})))))
            self.assertEqual(caught.exception.code, "ungrounded_argument")

    def test_drift_selection_without_a_drift_question_is_ungrounded(self):
        with self.assertRaises(IntakeFailure) as caught:
            plan(PROMPT, LLM(completion((TOOL_NAME, CAPTURED_ARGUMENTS), (DRIFT_TOOL_NAME, DRIFT_ARGUMENTS))))
        self.assertEqual(caught.exception.code, "ungrounded_argument")

    def test_unbounded_or_unknown_tool_calls_fail_closed(self):
        cases = {
            "three calls": (completion((TOOL_NAME, CAPTURED_ARGUMENTS), (DRIFT_TOOL_NAME, DRIFT_ARGUMENTS),
                                       (DRIFT_TOOL_NAME, DRIFT_ARGUMENTS)), "multiple_tool_calls"),
            "same tool twice": (completion((DRIFT_TOOL_NAME, DRIFT_ARGUMENTS), (DRIFT_TOOL_NAME, DRIFT_ARGUMENTS)),
                                "multiple_tool_calls"),
            "unknown tool": (completion(("delete_account", {})), "unauthorized_tool"),
        }
        for label, (body, code) in cases.items():
            with self.subTest(label), self.assertRaises(IntakeFailure) as caught:
                plan(COMBINED_TEXT, LLM(body))
            self.assertEqual(caught.exception.code, code)

    def test_extractor_reads_only_labelled_values(self):
        arguments = extract_drift_arguments(DRIFT_TEXT)
        self.assertEqual((arguments.feature_name, arguments.baseline_id),
                         ("account_behavior__account_tx_count_1h", "bl-r5gb-202604.account_tx_count_1h"))


class RenderingTests(unittest.TestCase):
    def test_combined_answer_names_each_specialist_and_copies_values(self):
        intake = plan(COMBINED_TEXT, LLM(completion((TOOL_NAME, CAPTURED_ARGUMENTS), (DRIFT_TOOL_NAME, DRIFT_ARGUMENTS))))
        result = {"status": "partial", "request_id": intake.request.request_id, "specialists": {
            "feature-rag-specialist": {"status": "ok", "features": {}, "fraud_score": {
                "model_name": "fraud-detector", "model_version": "1", "fraud_probability": 0.42, "fraud_prediction": 0,
                "feature_timestamp": "2026-05-31T23:00:00Z"}, "citations": [], "unavailable": []},
            "drift-specialist": {"status": "failed", "error": {"code": "a2a_transport_error"}},
        }}
        text = render_workflow_summary(result, intake)
        self.assertIn("### Từ feature-rag-specialist", text)
        self.assertIn("### Từ drift-specialist", text)
        self.assertIn("0.42", text)
        self.assertIn("a2a_transport_error", text)
        self.assertIn("drift (llm)", text)


if __name__ == "__main__":
    unittest.main()
