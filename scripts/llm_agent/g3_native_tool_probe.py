"""Build and validate a non-executing G3 native Agent-tool capability probe.

The probe stops at the OpenAI-compatible model response.  It never dispatches
the returned function, so it is safe to use for distinguishing a structured
``tool_calls`` response from text that merely looks like a tool call.

This module deliberately models the schema emitted by kagent 0.10.3's
``KAgentRemoteA2ATool``: one required string argument named ``request``.
Credentials are supplied only through ``EWALLET_VLLM_API_KEY`` and are never
written to the evidence report.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


FEATURE_RAG_TOOL = "ewallet_agents__NS__feature_rag_specialist"
DRIFT_TOOL = "ewallet_agents__NS__drift_specialist"
PSEUDO_TOOL_MARKERS = ("<tool_call>", "</tool_call>")


def agent_tool_schema(name: str, description: str) -> dict[str, Any]:
    """Return the exact public schema of a kagent 0.10.3 remote Agent tool."""

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"request": {"type": "string"}},
                "required": ["request"],
            },
        },
    }


def build_probe_request(
    *, model: str, wire_request: str, tool_choice: str = "auto"
) -> dict[str, Any]:
    """Build a one-tool, non-business model request for capability diagnosis."""

    if tool_choice not in {"auto", "required"}:
        raise ValueError("tool_choice must be auto or required")
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Call the supplied function once with request equal to the complete "
                    "two-line payload below. Return no prose.\n" + wire_request
                ),
            }
        ],
        "tools": [
            agent_tool_schema(
                FEATURE_RAG_TOOL, "Fraud Investigation / Evidence specialist"
            )
        ],
        "tool_choice": tool_choice,
        "temperature": 0,
        "max_tokens": 256,
        "stream": False,
    }


def validate_native_tool_response(
    response: object, *, expected_tool: str, expected_wire_request: str
) -> list[str]:
    """Fail closed on pseudo calls, invalid arguments, drift, or identity loss."""

    if not isinstance(response, dict):
        return ["response is not an object"]
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return ["response must contain exactly one choice"]
    choice = choices[0]
    if not isinstance(choice, dict):
        return ["choice is not an object"]
    message = choice.get("message")
    if not isinstance(message, dict):
        return ["choice.message is not an object"]

    errors: list[str] = []
    content = message.get("content")
    if isinstance(content, str) and any(marker in content for marker in PSEUDO_TOOL_MARKERS):
        errors.append("pseudo-tool markup was returned as assistant content")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        errors.append("exactly one structured tool_call is required")
        return errors
    if choice.get("finish_reason") != "tool_calls":
        errors.append("finish_reason must be tool_calls")

    call = calls[0]
    if not isinstance(call, dict) or call.get("type") != "function":
        errors.append("tool_call must be a function object")
        return errors
    function = call.get("function")
    if not isinstance(function, dict):
        errors.append("tool_call.function is not an object")
        return errors
    if function.get("name") != expected_tool:
        errors.append("unexpected tool name")
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        errors.append("function arguments must be a JSON string")
        return errors
    try:
        decoded = json.loads(arguments)
    except json.JSONDecodeError:
        errors.append("function arguments are not valid JSON")
        return errors
    if not isinstance(decoded, dict) or set(decoded) != {"request"}:
        errors.append("function arguments must contain only request")
        return errors
    if decoded["request"] != expected_wire_request:
        errors.append("request identity was not preserved byte-for-byte")
    return errors


def call_model(endpoint: str, key: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Call a loopback-forwarded endpoint without logging the bearer token."""

    request = Request(
        endpoint.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310: loopback enforced
            body = json.load(response)
            body["_probe_http_status"] = response.status
    except HTTPError as exc:
        body = {"error": exc.read().decode(errors="replace")[:500], "_probe_http_status": exc.code}
    except (URLError, TimeoutError, OSError) as exc:
        body = {"error": f"{type(exc).__name__}: {exc}", "_probe_http_status": None}
    body["_probe_latency_s"] = round(time.monotonic() - started, 3)
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", default="ewallet-fraud-llm")
    parser.add_argument("--wire-request-file", required=True)
    parser.add_argument("--tool-choice", choices=("auto", "required"), default="auto")
    parser.add_argument("--timeout", type=float, default=220.0)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    if not args.endpoint.startswith("http://127.0.0.1:"):
        parser.error("probe endpoint must be a loopback port-forward")
    key = os.environ.get("EWALLET_VLLM_API_KEY")
    if not key:
        parser.error("EWALLET_VLLM_API_KEY is required")

    wire_request = Path(args.wire_request_file).read_text(encoding="utf-8").rstrip("\n")
    payload = build_probe_request(
        model=args.model, wire_request=wire_request, tool_choice=args.tool_choice
    )
    response = call_model(args.endpoint, key, payload, args.timeout)
    errors = validate_native_tool_response(
        response, expected_tool=FEATURE_RAG_TOOL, expected_wire_request=wire_request
    )
    report = {
        "probe_contract": "g3-native-agent-tool/v1",
        "executes_tool": False,
        "request": payload,
        "response": response,
        "validation_errors": errors,
        "verdict": "PASS" if not errors else "FAIL",
    }
    Path(args.evidence).write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({"verdict": report["verdict"], "validation_errors": errors}))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
