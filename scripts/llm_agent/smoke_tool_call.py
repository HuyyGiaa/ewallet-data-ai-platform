"""R1 acceptance: OpenAI tool-call round trip against a LOCAL endpoint.

Runs tool call -> fixture tool result -> final reply, non-streamed or streamed,
against either vLLM directly (``--model ewallet-fraud-llm-v1``) or the LLM
agentgateway (``--model ewallet-fraud-llm``, the virtual model the kagent
ModelConfig uses). The tool result is a LOCAL TEST FIXTURE, never Feast/KServe.

Exit 0 only when the first turn is HTTP 200 with structurally valid
``tool_calls`` and the second turn is HTTP 200. ``--evidence`` writes a JSON
record (status, latency, backend header, token usage, validation errors) and
never the API key.

usage:
  EWALLET_VLLM_API_KEY=... python3 scripts/llm_agent/smoke_tool_call.py \
      --endpoint http://127.0.0.1:<port> --model ewallet-fraud-llm [--stream] \
      [--tool-choice required|auto] [--timeout 220] [--evidence out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TOOLS = [{
    "type": "function",
    "function": {
        "name": "feature_lookup",
        "description": "Retrieve a test transaction feature fixture.",
        "parameters": {
            "type": "object",
            "properties": {"transaction_id": {"type": "string"}},
            "required": ["transaction_id"],
            "additionalProperties": False,
        },
    },
}]
LEAK_MARKERS = ("<tool_call>", "</tool_call>", "<think>", "</think>")


def validate_tool_calls(message: dict[str, Any], tools: list[dict[str, Any]] = TOOLS) -> list[str]:
    """Structural checks of an assistant message carrying OpenAI tool_calls."""

    errors: list[str] = []
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return ["tool_calls missing or empty"]
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in tools}
    for index, call in enumerate(calls):
        where = f"tool_calls[{index}]"
        if not isinstance(call, dict):
            errors.append(f"{where} is not an object")
            continue
        if not isinstance(call.get("id"), str) or not call["id"]:
            errors.append(f"{where}.id missing")
        if call.get("type") != "function":
            errors.append(f"{where}.type is {call.get('type')!r}, expected 'function'")
        function = call.get("function") or {}
        name = function.get("name")
        if name not in schemas:
            errors.append(f"{where}.function.name {name!r} was not offered")
            continue
        raw = function.get("arguments")
        if not isinstance(raw, str):
            errors.append(f"{where}.function.arguments is not a JSON string")
            continue
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            errors.append(f"{where}.function.arguments is not valid JSON")
            continue
        schema = schemas[name]
        if not isinstance(arguments, dict):
            errors.append(f"{where}.function.arguments is not an object")
            continue
        missing = [key for key in schema.get("required", []) if key not in arguments]
        extra = [key for key in arguments if key not in schema.get("properties", {})]
        if missing:
            errors.append(f"{where} missing required arguments {missing}")
        if extra and schema.get("additionalProperties") is False:
            errors.append(f"{where} has unexpected arguments {extra}")
    content = message.get("content") or ""
    if any(marker in content for marker in LEAK_MARKERS):
        errors.append("raw tool-call or reasoning markup leaked into content")
    return errors


def assemble_stream(lines: Iterable[str]) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
    """Merge OpenAI SSE chat chunks into one assistant message."""

    message: dict[str, Any] = {"role": "assistant", "content": ""}
    calls: dict[int, dict[str, Any]] = {}
    finish, usage = None, None
    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            message["content"] += delta.get("content") or ""
            for part in delta.get("tool_calls") or []:
                call = calls.setdefault(part.get("index", 0), {"function": {"name": "", "arguments": ""}})
                if part.get("id"):
                    call["id"] = part["id"]
                if part.get("type"):
                    call["type"] = part["type"]
                function = part.get("function") or {}
                call["function"]["name"] += function.get("name") or ""
                call["function"]["arguments"] += function.get("arguments") or ""
            finish = choice.get("finish_reason") or finish
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return message, finish, usage


def call(endpoint: str, key: str, payload: dict[str, Any], *, timeout: float, stream: bool) -> dict[str, Any]:
    body = dict(payload, stream=stream)
    if stream:
        body["stream_options"] = {"include_usage": True}
    request = Request(endpoint.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                      method="POST")
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 (local-only endpoint checked)
            headers = {k.lower(): v for k, v in response.headers.items()}
            if stream:
                first_byte = None
                lines = []
                for raw in response:
                    first_byte = first_byte or time.monotonic()
                    lines.append(raw.decode())
                message, finish, usage = assemble_stream(lines)
            else:
                payload_out = json.load(response)
                first_byte = None
                choice = payload_out["choices"][0]
                message, finish, usage = choice["message"], choice.get("finish_reason"), payload_out.get("usage")
            return {"status": response.status, "message": message, "finish_reason": finish, "usage": usage,
                    "backend": headers.get("x-ewallet-backend"),
                    "latency_s": round(time.monotonic() - started, 3),
                    "first_byte_s": round(first_byte - started, 3) if first_byte else None}
    except HTTPError as exc:
        return {"status": exc.code, "error": exc.read().decode(errors="replace")[:300],
                "latency_s": round(time.monotonic() - started, 3)}
    except (URLError, TimeoutError) as exc:
        return {"status": None, "error": f"unreachable or client timeout: {exc}",
                "latency_s": round(time.monotonic() - started, 3)}


def run(endpoint: str, key: str, model: str, *, tool_choice: str, stream: bool, timeout: float) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are in a test environment. Use the provided feature_lookup tool; "
                                      "never invent real fraud data."},
        {"role": "user", "content": "Use feature_lookup for transaction tx-demo-001, then summarize the returned fixture."},
    ]
    report: dict[str, Any] = {"model": model, "stream": stream, "tool_choice": tool_choice,
                              "fixture_only": True, "verdict": "FAIL"}
    first = call(endpoint, key, {"model": model, "messages": messages, "tools": TOOLS,
                                 "tool_choice": tool_choice, "temperature": 0, "max_tokens": 256},
                 timeout=timeout, stream=stream)
    report["first_turn"] = {k: v for k, v in first.items() if k != "message"}
    if first["status"] != 200:
        return report
    errors = validate_tool_calls(first["message"])
    report["first_turn"]["tool_calls"] = first["message"].get("tool_calls")
    report["tool_call_errors"] = errors
    if errors:
        return report
    messages.append({"role": "assistant", "content": first["message"].get("content") or None,
                     "tool_calls": first["message"]["tool_calls"]})
    for item in first["message"]["tool_calls"]:
        arguments = json.loads(item["function"]["arguments"])
        # Intentional local mock: NOT a real Feast integration or proof.
        result = {"transaction_id": arguments.get("transaction_id"), "source": "LOCAL_TEST_FIXTURE_ONLY",
                  "is_real_fraud_score": False, "velocity_5m": 3}
        messages.append({"role": "tool", "tool_call_id": item["id"], "content": json.dumps(result)})
    final = call(endpoint, key, {"model": model, "messages": messages, "temperature": 0, "max_tokens": 256},
                 timeout=timeout, stream=stream)
    report["second_turn"] = {k: v for k, v in final.items() if k != "message"}
    if final["status"] == 200:
        report["second_turn"]["reply_chars"] = len(final["message"].get("content") or "")
        report["verdict"] = "PASS"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default="ewallet-fraud-llm-v1")
    parser.add_argument("--tool-choice", choices=["required", "auto"], default="required")
    parser.add_argument("--stream", action="store_true")
    # Longer than the gateway entry budget (local 180 s), so the gateway, not
    # this client, reports a slow backend.
    parser.add_argument("--timeout", type=float, default=220.0)
    parser.add_argument("--evidence")
    args = parser.parse_args(argv)
    if not args.endpoint.startswith("http://127.0.0.1:"):
        parser.error("local-only smoke: endpoint must be http://127.0.0.1:<port>")
    key = os.environ.get("EWALLET_VLLM_API_KEY")
    if not key:
        parser.error("EWALLET_VLLM_API_KEY missing; use a local demo token")
    report = run(args.endpoint, key, args.model, tool_choice=args.tool_choice, stream=args.stream,
                 timeout=args.timeout)
    report["endpoint"] = args.endpoint
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.evidence:
        with open(args.evidence, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
