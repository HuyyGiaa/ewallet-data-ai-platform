from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from ai_platform.llm_agent.business_request import (
    MAX_BUSINESS_REQUEST_BYTES,
    G3BusinessRequest,
    a2a_message_send,
    business_request_from_a2a,
)


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
FEATURES = (
    "account_behavior__account_tx_count_1h",
    "account_behavior__account_tx_count_24h",
    "user_behavior__user_avg_amount_30d",
    "merchant_behavior__merchant_tx_count_24h",
)


def business(**overrides) -> G3BusinessRequest:
    payload = {
        "request_id": "g3-functional-closure-001",
        "target_agent": "feature-rag-specialist",
        "capabilities": ["fraud-evidence"],
        "requested_tools": ["feature_lookup", "fraud_scoring"],
        "transaction": TRANSACTION,
        "feature_names": FEATURES,
    }
    payload.update(overrides)
    return G3BusinessRequest.model_validate(payload)


class G3BusinessRequestTests(unittest.TestCase):
    def test_round_trip_is_canonical_bounded_and_identity_bound(self):
        request = business()
        wire = request.to_compact_json()
        self.assertLessEqual(len(wire.encode()), MAX_BUSINESS_REQUEST_BYTES)
        self.assertEqual(wire, json.dumps(json.loads(wire), sort_keys=True, separators=(",", ":")))
        payload = a2a_message_send(request)
        self.assertEqual(business_request_from_a2a(payload), request)
        self.assertEqual(
            payload["params"]["message"]["parts"][0]["text"],
            f"CALL_AGENT=feature-rag-specialist\nREQUEST_JSON={wire}",
        )

    def test_unrequested_drift_and_unknown_transaction_fields_fail_closed(self):
        with self.assertRaises(ValidationError):
            business(requested_tools=["feature_lookup", "drift_detection"])
        with self.assertRaises(ValidationError):
            business(transaction={**TRANSACTION, "invented": True})
        with self.assertRaises(ValidationError):
            business(target_agent="drift-specialist")

    def test_feature_projection_and_knowledge_query_are_consistent(self):
        with self.assertRaises(ValidationError):
            business(feature_names=[])
        with self.assertRaises(ValidationError):
            business(requested_tools=["fraud_scoring"], feature_names=FEATURES)
        with self.assertRaises(ValidationError):
            business(requested_tools=["knowledge_search"], feature_names=[])
        request = business(
            requested_tools=["knowledge_search"],
            feature_names=[],
            knowledge_query="payment velocity policy",
        )
        self.assertEqual(request.knowledge_query, "payment velocity policy")

    def test_a2a_rejects_noncanonical_or_mismatched_payload(self):
        payload = a2a_message_send(business())
        payload["params"]["message"]["parts"][0]["text"] += "\n"
        with self.assertRaises(ValueError):
            business_request_from_a2a(payload)
        payload = a2a_message_send(business())
        payload["id"] = "other"
        with self.assertRaises(ValueError):
            business_request_from_a2a(payload)


if __name__ == "__main__":
    unittest.main()
