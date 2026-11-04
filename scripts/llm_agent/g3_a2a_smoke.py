"""Run one bounded A2A JSON-RPC request through a local port-forward.

This client intentionally accepts only ``127.0.0.1`` endpoints.  Create the
port-forward separately so Kubernetes access remains explicit and auditable.
It never reads credentials and writes the exact JSON response plus a small
timing record when requested.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ai_platform.llm_agent.business_request import business_request_from_a2a


TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected", "unknown"})


def _local_endpoint(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise argparse.ArgumentTypeError("endpoint must be http://127.0.0.1:<port>/")
    if parsed.port is None:
        raise argparse.ArgumentTypeError("endpoint must include an explicit port")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", type=_local_endpoint, default="http://127.0.0.1:18083/")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--timing", type=Path)
    parser.add_argument("--timeout", type=float, default=650.0)
    parser.add_argument("--poll-timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()

    payload = json.loads(args.request.read_text(encoding="utf-8"))
    if payload.get("jsonrpc") != "2.0" or payload.get("method") != "message/send":
        parser.error("request must be an A2A JSON-RPC 2.0 message/send payload")
    try:
        business_request_from_a2a(payload)
    except ValueError as exc:
        parser.error(str(exc))

    def send(message: dict[str, object], timeout: float) -> tuple[int | None, bytes]:
        request = Request(
            args.endpoint,
            data=json.dumps(message, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - local-only
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()
        except (URLError, TimeoutError, OSError) as exc:
            return None, json.dumps({"client_error": f"{type(exc).__name__}: {exc}"}).encode()

    started = time.monotonic()
    status, body = send(payload, args.timeout)

    polls = 0
    terminal_state: str | None = None
    try:
        decoded = json.loads(body)
        task = decoded.get("result", {})
        task_id = task.get("id") if task.get("kind") == "task" else None
        terminal_state = task.get("status", {}).get("state")
    except (json.JSONDecodeError, AttributeError):
        decoded, task_id = None, None

    deadline = time.monotonic() + args.poll_timeout
    while status == 200 and task_id and terminal_state not in TERMINAL_STATES:
        if time.monotonic() >= deadline:
            terminal_state = "timeout"
            break
        time.sleep(args.poll_interval)
        polls += 1
        poll = {
            "jsonrpc": "2.0",
            "id": f"{payload['id']}-poll-{polls}",
            "method": "tasks/get",
            "params": {"id": task_id},
        }
        status, body = send(poll, min(args.timeout, max(1.0, deadline - time.monotonic())))
        try:
            decoded = json.loads(body)
            terminal_state = decoded.get("result", {}).get("status", {}).get("state")
        except (json.JSONDecodeError, AttributeError):
            break

    elapsed = round(time.monotonic() - started, 3)
    args.response.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = json.loads(body)
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    except json.JSONDecodeError:
        rendered = body.decode(errors="replace")
    args.response.write_text(rendered, encoding="utf-8")
    timing = {
        "http_status": status,
        "elapsed_seconds": elapsed,
        "polls": polls,
        "terminal_state": terminal_state,
        "response": str(args.response),
    }
    if args.timing:
        args.timing.parent.mkdir(parents=True, exist_ok=True)
        args.timing.write_text(json.dumps(timing, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(timing, indent=2))
    return 0 if status == 200 and terminal_state == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
