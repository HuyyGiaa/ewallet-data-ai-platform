"""Agent integration: typed drift and combined-workflow contracts (pure Pydantic, no network)."""

from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from ai_platform.llm_agent.business_request import (
    MONITORED_DRIFT_FEATURES,
    G3BusinessRequest,
    G3DriftRequest,
    G3WorkflowRequest,
    a2a_message_send,
    business_request_from_a2a,
    drift_request_from_a2a,
    is_typed_wire_text,
    member_request_id,
    typed_request_from_a2a,
)
from ai_platform.llm_agent.tools.drift import MONITORED_FEATURES

TRANSACTION = {
    "transaction_id": "dc58ff4e-b5f7-4486-b1f8-4c31383741a9",
    "event_timestamp": "2026-06-01T00:00:00+00:00",
    "user_id": "55561d92-d042-401c-b02a-b03e3ed280f7",
    "account_id": "2d48f147-f98c-4819-b183-f94ca049c175",
    "device_id": "e7765b81-c9f9-4147-aa00-46f34de1d7da",
    "merchant_id": "e3e4aced-6b0d-40af-be5c-eef9255204c9",
    "counterparty_account_id": None,
    "type": "payment",
    "amount": 275465.53,
    "currency": "VND",
    "channel": "app",
}
FEATURE = "account_behavior__account_tx_count_1h"
WINDOW = {"start": "2026-05-01T00:00:00+00:00", "end": "2026-05-31T00:00:00+00:00"}


def fraud(request_id: str = "g3-001") -> G3BusinessRequest:
    return G3BusinessRequest(
        request_id=request_id, target_agent="feature-rag-specialist", capabilities=("fraud-evidence",),
        requested_tools=("feature_lookup", "fraud_scoring"), transaction=TRANSACTION, feature_names=(FEATURE,),
    )


def drift(request_id: str = "drift-001", **changes) -> G3DriftRequest:
    value = {"request_id": request_id, "feature_name": FEATURE, "baseline_id": "bl-r5gb-202604.account_tx_count_1h",
             "current_window": WINDOW}
    value.update(changes)
    return G3DriftRequest.model_validate(value)


def workflow(request_id: str = "wf-001") -> G3WorkflowRequest:
    return G3WorkflowRequest(request_id=request_id, fraud=fraud(member_request_id(request_id, "fraud")),
                             drift=drift(member_request_id(request_id, "drift")))


class DriftRequestTests(unittest.TestCase):
    def test_monitored_features_match_the_drift_tool(self):
        self.assertEqual(MONITORED_DRIFT_FEATURES, MONITORED_FEATURES)

    def test_valid_request_has_fixed_route_and_exact_tool_arguments(self):
        request = drift()
        self.assertEqual((request.target_agent, request.capabilities, request.requested_tools),
                         ("drift-specialist", ("drift",), ("drift_detection",)))
        self.assertEqual(request.tool_arguments(), {
            "feature_name": FEATURE, "baseline_id": "bl-r5gb-202604.account_tx_count_1h",
            "current_window": {"start": "2026-05-01T00:00:00Z", "end": "2026-05-31T00:00:00Z"},
        })
        self.assertTrue(request.to_wire_text().startswith("CALL_AGENT=drift-specialist\nREQUEST_JSON="))

    def test_invalid_business_arguments_are_rejected(self):
        cases = {
            "unsupported feature": {"feature_name": "account_behavior__unknown"},
            "mutable baseline": {"baseline_id": "latest"},
            "mutable baseline case": {"baseline_id": "Current"},
            "baseline with space": {"baseline_id": "bl 1"},
            "reversed window": {"current_window": {"start": WINDOW["end"], "end": WINDOW["start"]}},
            "window too long": {"current_window": {"start": "2026-01-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"}},
            "naive window": {"current_window": {"start": "2026-05-01T00:00:00", "end": "2026-05-02T00:00:00"}},
            "extra field": {"top_k": 3},
            "other target": {"target_agent": "feature-rag-specialist"},
            "other tool": {"requested_tools": ("feature_lookup",)},
        }
        for label, change in cases.items():
            with self.subTest(label), self.assertRaises(ValidationError):
                drift(**change)


class WorkflowRequestTests(unittest.TestCase):
    def test_member_ids_are_derived_and_enforced(self):
        request = workflow()
        self.assertEqual((request.fraud.request_id, request.drift.request_id), ("wf-001.fraud", "wf-001.drift"))
        with self.assertRaises(ValidationError):
            G3WorkflowRequest(request_id="wf-001", fraud=fraud("wf-001.fraud"), drift=drift("other.drift"))
        with self.assertRaises(ValidationError):
            G3WorkflowRequest(request_id="wf-001", fraud=fraud("wf-001"), drift=drift("wf-001.drift"))

    def test_workflow_uses_its_own_wire_route(self):
        self.assertTrue(workflow().to_wire_text().startswith("CALL_WORKFLOW=combined\nREQUEST_JSON="))
        self.assertTrue(is_typed_wire_text(workflow().to_wire_text()))
        self.assertTrue(is_typed_wire_text(drift().to_wire_text()))
        self.assertFalse(is_typed_wire_text("Hãy phân tích drift"))


def with_text(payload: dict, text: str) -> dict:
    value = json.loads(json.dumps(payload))
    value["params"]["message"]["parts"][0]["text"] = text
    return value


class WireParsingTests(unittest.TestCase):
    def test_each_contract_round_trips_and_parsers_accept_only_their_kind(self):
        for request in (fraud(), drift(), workflow()):
            with self.subTest(request.contract_version):
                self.assertEqual(typed_request_from_a2a(a2a_message_send(request)), request)
        self.assertEqual(drift_request_from_a2a(a2a_message_send(drift())), drift())
        self.assertEqual(business_request_from_a2a(a2a_message_send(fraud())), fraud())
        for parser, other in ((business_request_from_a2a, drift()), (drift_request_from_a2a, fraud()),
                              (drift_request_from_a2a, workflow()), (business_request_from_a2a, workflow())):
            with self.subTest(parser=parser.__name__, other=other.contract_version), self.assertRaises(ValueError):
                parser(a2a_message_send(other))

    def test_non_canonical_or_mismatched_payloads_fail_closed(self):
        payload = a2a_message_send(drift())
        text = payload["params"]["message"]["parts"][0]["text"]
        cases = {
            "id mismatch": {**payload, "id": "other"},
            "route mismatch": json.loads(json.dumps(payload).replace("CALL_AGENT=drift-specialist", "CALL_AGENT=feature-rag-specialist")),
            "non canonical json": with_text(payload, text.replace(",", ", ", 1)),
            "unknown version": json.loads(json.dumps(payload).replace("g3-drift-request/v1", "g3-drift-request/v9")),
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises((ValueError, ValidationError)):
                typed_request_from_a2a(value)


if __name__ == "__main__":
    unittest.main()
