from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "g3_native_tool_probe", ROOT / "scripts/llm_agent/g3_native_tool_probe.py"
)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

WIRE = (
    "CALL_AGENT=feature-rag-specialist\n"
    'REQUEST_JSON={"request_id":"g3-native-probe","transaction_id":"tx-001"}'
)


def response(*, name=probe.FEATURE_RAG_TOOL, arguments=None, content=None, finish="tool_calls"):
    if arguments is None:
        arguments = json.dumps({"request": WIRE})
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
            }
        ]
    }


class G3NativeToolProbeTests(unittest.TestCase):
    def test_registration_schema_matches_kagent_0103_agent_tool(self):
        schema = probe.agent_tool_schema(probe.FEATURE_RAG_TOOL, "specialist")
        self.assertEqual(schema["function"]["name"], probe.FEATURE_RAG_TOOL)
        self.assertEqual(
            schema["function"]["parameters"],
            {
                "type": "object",
                "properties": {"request": {"type": "string"}},
                "required": ["request"],
            },
        )

    def test_model_request_offers_exact_agent_tool_and_explicit_choice(self):
        request = probe.build_probe_request(
            model="ewallet-fraud-llm", wire_request=WIRE, tool_choice="auto"
        )
        self.assertEqual(request["tool_choice"], "auto")
        self.assertEqual(len(request["tools"]), 1)
        self.assertEqual(request["tools"][0]["function"]["name"], probe.FEATURE_RAG_TOOL)
        self.assertNotIn("drift", json.dumps(request).lower())

    def test_structured_call_preserves_transaction_identity_byte_for_byte(self):
        self.assertEqual(
            probe.validate_native_tool_response(
                response(), expected_tool=probe.FEATURE_RAG_TOOL, expected_wire_request=WIRE
            ),
            [],
        )

    def test_text_only_pseudo_tool_response_is_rejected(self):
        payload = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": '<tool_call>{"name":"x"}</tool_call>',
                        "tool_calls": None,
                    },
                }
            ]
        }
        errors = probe.validate_native_tool_response(
            payload, expected_tool=probe.FEATURE_RAG_TOOL, expected_wire_request=WIRE
        )
        self.assertIn("pseudo-tool markup was returned as assistant content", errors)
        self.assertIn("exactly one structured tool_call is required", errors)

    def test_invalid_arguments_and_unsolicited_drift_fail_closed(self):
        invalid = probe.validate_native_tool_response(
            response(arguments='{"request":"unterminated}'),
            expected_tool=probe.FEATURE_RAG_TOOL,
            expected_wire_request=WIRE,
        )
        self.assertEqual(invalid, ["function arguments are not valid JSON"])
        drift = probe.validate_native_tool_response(
            response(name=probe.DRIFT_TOOL),
            expected_tool=probe.FEATURE_RAG_TOOL,
            expected_wire_request=WIRE,
        )
        self.assertEqual(drift, ["unexpected tool name"])

    def test_identity_mutation_and_non_terminal_finish_fail_closed(self):
        errors = probe.validate_native_tool_response(
            response(arguments=json.dumps({"request": WIRE + "\n"}), finish="stop"),
            expected_tool=probe.FEATURE_RAG_TOOL,
            expected_wire_request=WIRE,
        )
        self.assertIn("finish_reason must be tool_calls", errors)
        self.assertIn("request identity was not preserved byte-for-byte", errors)


if __name__ == "__main__":
    unittest.main()
