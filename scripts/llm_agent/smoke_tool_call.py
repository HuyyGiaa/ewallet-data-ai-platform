"""Optional user-operated LOCAL-only vLLM tool-call protocol smoke.

This script never calls the production Feast/KServe backend. It uses a clearly
identified local fixture to exercise OpenAI tool-call -> tool result -> reply.
"""

from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def call(endpoint: str, key: str, payload: dict) -> dict:
    request = Request(
        endpoint.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from local vLLM; check /health and model logs") from exc
    except URLError as exc:
        raise RuntimeError("local vLLM is not reachable; check container/port") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default="ewallet-fraud-llm-v1")
    parser.add_argument("--tool-choice", choices=["required", "auto"], default="required")
    args = parser.parse_args()
    if not args.endpoint.startswith("http://127.0.0.1:"):
        parser.error("local-only smoke: endpoint must be http://127.0.0.1:<port>")
    key = os.environ.get("EWALLET_VLLM_API_KEY")
    if not key:
        parser.error("EWALLET_VLLM_API_KEY missing; use a local demo token")
    messages = [
        {"role": "system", "content": "You are in a test environment. Use the provided feature_lookup tool; never invent real fraud data."},
        {"role": "user", "content": "Use feature_lookup for transaction tx-demo-001, then summarize the returned fixture."},
    ]
    tools = [{
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
    first = call(args.endpoint, key, {
        "model": args.model, "messages": messages, "tools": tools,
        "tool_choice": args.tool_choice, "temperature": 0, "max_tokens": 256,
    })
    choice = first["choices"][0]["message"]
    tool_calls = choice.get("tool_calls") or []
    if not tool_calls:
        print(json.dumps({"status": "NO_TOOL_CALL", "mode": args.tool_choice,
                          "note": "Auto may legitimately choose no tool; use --tool-choice required for protocol smoke"}))
        return 1
    messages.append(choice)
    for item in tool_calls:
        fn = item.get("function", {})
        if fn.get("name") != "feature_lookup":
            raise RuntimeError(f"unexpected tool: {fn.get('name')}")
        params = json.loads(fn.get("arguments", "{}"))
        if params.get("transaction_id") != "tx-demo-001":
            raise RuntimeError("unexpected arguments; refuse to execute")
        # Intentional local mock: NOT a real Feast integration or proof.
        result = {"transaction_id": "tx-demo-001", "source": "LOCAL_TEST_FIXTURE_ONLY",
                  "is_real_fraud_score": False, "velocity_5m": 3}
        messages.append({"role": "tool", "tool_call_id": item["id"],
                         "content": json.dumps(result)})
    final = call(args.endpoint, key, {
        "model": args.model, "messages": messages, "temperature": 0, "max_tokens": 256,
    })
    print(json.dumps({"status": "LOCAL_TOOL_PROTOCOL_PASS", "tool_calls": len(tool_calls),
                      "fixture_only": True,
                      "assistant_reply": final["choices"][0]["message"].get("content")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
