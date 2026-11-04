"""N11 P0: bounded natural-language intake (LLM tool selection + grounding)."""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from ai_platform.llm_agent.nl_intake import (
    DEFAULT_FEATURE_NAMES,
    DEFAULT_POLICY_QUERY,
    TOOL_NAME,
    IntakeFailure,
    OpenAIToolPlanner,
    business_request,
    ground_arguments,
    labelled_identifiers,
    plan,
    request_id_for,
)


PROMPT = (
    "Hãy đánh giá rủi ro gian lận cho giao dịch payment dc58ff4e-b5f7-4486-b1f8-4c31383741a9 "
    "(user 55561d92-d042-401c-b02a-b03e3ed280f7, account 2d48f147-f98c-4819-b183-f94ca049c175, "
    "device e7765b81-c9f9-4147-aa00-46f34de1d7da, merchant e3e4aced-6b0d-40af-be5c-eef9255204c9, "
    "amount 275465.53 VND, channel app, event_timestamp 2026-06-01T00:00:00Z): lấy feature của user/account, "
    "gọi mô hình dự đoán gian lận, giải thích mức rủi ro và trích dẫn chính sách liên quan."
)
# Exact arguments produced by Qwen3-0.6B through agentgateway -> vLLM (hermes
# parser) on GKE, N11 probe `A-rootcause/llm-tool-probe.json`, 2026-11-04.
CAPTURED_ARGUMENTS = {
    "transaction_id": "dc58ff4e-b5f7-4486-b1f8-4c31383741a9",
    "transaction_type": "payment",
    "user_id": "55561d92-d042-401c-b02a-b03e3ed280f7",
    "account_id": "2d48f147-f98c-4819-b183-f94ca049c175",
    "device_id": "e7765b81-c9f9-4147-aa00-46f34de1d7da",
    "merchant_id": "e3e4aced-6b0d-40af-be5c-eef9255204c9",
    "amount_vnd": 275465.53,
    "channel": "app",
    "event_timestamp": "2026-06-01T00:00:00Z",
}
KEY = "test-client-key"


def completion(arguments=CAPTURED_ARGUMENTS, *, name=TOOL_NAME, calls=1, raw=None):
    tool_calls = [{
        "id": f"chatcmpl-tool-{index}",
        "type": "function",
        "function": {"name": name, "arguments": raw if raw is not None else json.dumps(arguments)},
    } for index in range(calls)]
    return {
        "id": "chatcmpl-n11",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                     "message": {"role": "assistant", "content": None if calls else "Xin chào", "tool_calls": tool_calls}}],
        "usage": {"prompt_tokens": 594, "completion_tokens": 256},
    }


class RecordingLLM:
    def __init__(self, body=None, status=200, exc=None):
        self.body, self.status, self.exc = body if body is not None else completion(), status, exc
        self.requests: list[httpx.Request] = []

    def transport(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.exc is not None:
                raise self.exc
            return httpx.Response(self.status, json=self.body)
        return httpx.MockTransport(handler)

    def planner(self, key=KEY):
        return OpenAIToolPlanner(key_reader=lambda: key, transport=self.transport())


def run(coro):
    return asyncio.run(coro)


class GroundingTests(unittest.TestCase):
    def test_labelled_identifiers_assign_every_role_from_the_text(self):
        self.assertEqual(labelled_identifiers(PROMPT), {
            "transaction_id": CAPTURED_ARGUMENTS["transaction_id"],
            "user_id": CAPTURED_ARGUMENTS["user_id"],
            "account_id": CAPTURED_ARGUMENTS["account_id"],
            "device_id": CAPTURED_ARGUMENTS["device_id"],
            "merchant_id": CAPTURED_ARGUMENTS["merchant_id"],
        })

    def test_captured_real_llm_arguments_are_accepted(self):
        arguments = ground_arguments(PROMPT, CAPTURED_ARGUMENTS)
        self.assertEqual(arguments.transaction_id, CAPTURED_ARGUMENTS["transaction_id"])
        self.assertEqual(arguments.amount_vnd, 275465.53)

    def test_ungrounded_or_swapped_values_are_rejected(self):
        cases = {
            "invented id": {"transaction_id": "00000000-0000-4000-8000-000000000000"},
            "swapped user/account": {"user_id": CAPTURED_ARGUMENTS["account_id"],
                                     "account_id": CAPTURED_ARGUMENTS["user_id"]},
            "wrong amount": {"amount_vnd": 275465.0},
            "wrong timestamp": {"event_timestamp": "2026-06-02T00:00:00Z"},
            "wrong type": {"transaction_type": "transfer"},
            "wrong channel": {"channel": "web"},
            "dropped merchant": {"merchant_id": None},
        }
        for label, change in cases.items():
            with self.subTest(label):
                with self.assertRaises(IntakeFailure) as caught:
                    ground_arguments(PROMPT, {**CAPTURED_ARGUMENTS, **change})
                self.assertEqual(caught.exception.code, "ungrounded_argument")

    def test_unknown_argument_keys_fail_schema_validation(self):
        with self.assertRaises(IntakeFailure) as caught:
            ground_arguments(PROMPT, {**CAPTURED_ARGUMENTS, "currency": "VND"})
        self.assertEqual(caught.exception.code, "invalid_tool_arguments")

    def test_missing_timestamp_in_text_is_rejected_not_defaulted(self):
        text = PROMPT.replace(", event_timestamp 2026-06-01T00:00:00Z", "")
        with self.assertRaises(IntakeFailure) as caught:
            ground_arguments(text, CAPTURED_ARGUMENTS)
        self.assertEqual(caught.exception.code, "missing_or_ambiguous_field")

    def test_two_transactions_for_one_role_are_ambiguous(self):
        text = PROMPT.replace("(user", "giao dịch 11111111-2222-4333-8444-555555555555 (user")
        with self.assertRaises(IntakeFailure) as caught:
            labelled_identifiers(text)
        self.assertEqual(caught.exception.code, "ambiguous_identifier")

    def test_business_request_preserves_typed_contract_and_fixed_projection(self):
        request = business_request(PROMPT, ground_arguments(PROMPT, CAPTURED_ARGUMENTS), "nl-abc")
        self.assertEqual(request.requested_tools, ("feature_lookup", "fraud_scoring", "knowledge_search"))
        self.assertEqual(request.feature_names, DEFAULT_FEATURE_NAMES)
        self.assertEqual(request.knowledge_query, DEFAULT_POLICY_QUERY)
        self.assertEqual(request.transaction.merchant_id, CAPTURED_ARGUMENTS["merchant_id"])
        self.assertTrue(request.to_wire_text().startswith("CALL_AGENT=feature-rag-specialist\nREQUEST_JSON="))

    def test_policy_citations_only_when_requested(self):
        text = PROMPT.replace(" và trích dẫn chính sách liên quan", "")
        request = business_request(text, ground_arguments(text, CAPTURED_ARGUMENTS), "nl-abc")
        self.assertEqual(request.requested_tools, ("feature_lookup", "fraud_scoring"))
        self.assertIsNone(request.knowledge_query)

    def test_request_id_is_stable_and_bounded(self):
        self.assertEqual(request_id_for("ctx", "msg"), request_id_for("ctx", "msg"))
        self.assertNotEqual(request_id_for("ctx", "msg"), request_id_for("ctx", "msg2"))
        self.assertLessEqual(len(request_id_for("c" * 500, "m" * 500)), 128)


class PlannerTests(unittest.TestCase):
    def test_plan_sends_one_native_tool_request_and_builds_typed_request(self):
        llm = RecordingLLM()
        result = run(plan(PROMPT, planner=llm.planner(), context_id="ctx-1", message_id="msg-1"))
        self.assertEqual(len(llm.requests), 1)
        sent = llm.requests[0]
        self.assertEqual(str(sent.url), "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1/chat/completions")
        self.assertEqual(sent.headers["authorization"], f"Bearer {KEY}")
        body = json.loads(sent.content)
        self.assertEqual([tool["function"]["name"] for tool in body["tools"]], [TOOL_NAME])
        self.assertEqual((body["tool_choice"], body["temperature"]), ("auto", 0))
        self.assertEqual(result.selection.name, TOOL_NAME)
        self.assertEqual(result.request.request_id, request_id_for("ctx-1", "msg-1"))

    def test_question_without_identifiers_never_reaches_the_llm(self):
        llm = RecordingLLM()
        with self.assertRaises(IntakeFailure) as caught:
            run(plan("Thời tiết hôm nay thế nào?", planner=llm.planner(), context_id="c", message_id="m"))
        self.assertEqual(caught.exception.code, "missing_transaction_identifiers")
        self.assertEqual(llm.requests, [])

    def test_llm_negative_responses_fail_closed(self):
        cases = {
            "no tool": (RecordingLLM(completion(calls=0)), "unsupported_request"),
            "unauthorized tool": (RecordingLLM(completion(name="delete_account")), "unauthorized_tool"),
            "two tools": (RecordingLLM(completion(calls=2)), "multiple_tool_calls"),
            "broken json": (RecordingLLM(completion(raw='{"transaction_id": "dc58"')), "invalid_tool_arguments"),
            "gateway 503": (RecordingLLM(status=503, body={"error": "x"}), "llm_unavailable"),
            "gateway 401": (RecordingLLM(status=401, body={"error": "x"}), "llm_unavailable"),
            "timeout": (RecordingLLM(exc=httpx.ReadTimeout("slow")), "llm_timeout"),
            "transport": (RecordingLLM(exc=httpx.ConnectError("down")), "llm_unavailable"),
            "not a completion": (RecordingLLM({"unexpected": True}), "llm_invalid_response"),
        }
        for label, (llm, code) in cases.items():
            with self.subTest(label):
                with self.assertRaises(IntakeFailure) as caught:
                    run(plan(PROMPT, planner=llm.planner(), context_id="c", message_id="m"))
                self.assertEqual(caught.exception.code, code)

    def test_missing_credential_is_explicit(self):
        llm = RecordingLLM()
        with self.assertRaises(IntakeFailure) as caught:
            run(plan(PROMPT, planner=llm.planner(key=""), context_id="c", message_id="m"))
        self.assertEqual(caught.exception.code, "llm_credential_unavailable")
        self.assertEqual(llm.requests, [])

    def test_only_the_approved_gateway_route_is_accepted(self):
        for url in (
            "http://vllm-primary.ewallet-agents.svc.cluster.local:8000/v1",
            "https://agentgateway.ewallet-agents.svc.cluster.local:3000/v1",
            "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v2",
            "http://user:pw@agentgateway.ewallet-agents.svc.cluster.local:3000/v1",
        ):
            with self.subTest(url), self.assertRaises(ValueError):
                OpenAIToolPlanner(base_url=url, key_reader=lambda: KEY)


if __name__ == "__main__":
    unittest.main()
