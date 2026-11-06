"""Send a bounded stream of A2A tasks and print one JSON line per task.

Runs inside a Job, so traffic goes through a Service and reaches every replica; a
port-forward would pin all of it to one pod. Standard library only: the file is mounted
into an existing image. Every task is printed, including failures, and analysis happens
elsewhere. A task counts as a success only when it reaches ``completed``.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected", "unknown"})
Post = Callable[[Mapping[str, Any], float], Mapping[str, Any]]


def http_post(url: str) -> Post:
    """Return a JSON-RPC sender bound to one URL."""

    def post(message: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
        request = Request(url, data=json.dumps(message).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())

    return post


def task_payload(template: str, task_id: str) -> dict[str, Any]:
    """Fill the fixture; a new ID per task keeps idempotency from replaying a result."""

    return json.loads(template.replace("__TASK_ID__", task_id))


def task_state(reply: Mapping[str, Any]) -> tuple[str, str | None]:
    """Return the task state and its ID from a JSON-RPC reply."""

    result = reply.get("result")
    if not isinstance(result, Mapping):
        return "rpc_error", None
    status = result.get("status")
    state = status.get("state") if isinstance(status, Mapping) else None
    return str(state or "completed" if result.get("kind") == "message" else state or "unknown"), result.get("id")


def run_task(post: Post, template: str, task_id: str, *, timeout: float, poll_timeout: float, poll_interval: float,
             clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Run one task to a terminal state and describe it.

    Never raises for a request failure: the failure is the measurement.
    """

    started = clock()
    row: dict[str, Any] = {"id": task_id, "started": started, "state": "transport_error", "error": None}
    try:
        state, remote_id = task_state(post(task_payload(template, task_id), timeout))
        deadline = clock() + poll_timeout
        while state not in TERMINAL_STATES and state != "rpc_error" and remote_id and clock() < deadline:
            sleep(poll_interval)
            state, _ = task_state(post({"jsonrpc": "2.0", "id": task_id, "method": "tasks/get",
                                        "params": {"id": remote_id}}, timeout))
        row["state"] = state
    except (HTTPError, URLError, OSError, ValueError) as error:
        row["error"] = type(error).__name__
    row["finished"] = clock()
    row["e2e_ms"] = 1000 * (row["finished"] - started)
    row["ok"] = row["state"] == "completed"
    return row


def run_load(post: Post, template: str, *, run_id: str, concurrency: int, duration: float, max_requests: int,
             timeout: float, poll_timeout: float, poll_interval: float, emit: Callable[[str], None] = print) -> int:
    """Keep ``concurrency`` tasks open until the duration or request cap ends; return tasks sent."""

    lock = threading.Lock()
    deadline = time.monotonic() + duration
    sent = 0

    def worker() -> None:
        nonlocal sent
        while time.monotonic() < deadline:
            with lock:
                if sent >= max_requests:
                    return
                sent += 1
                task_id = f"{run_id}-{sent:04d}"
            row = run_task(post, template, task_id, timeout=timeout, poll_timeout=poll_timeout, poll_interval=poll_interval)
            with lock:
                emit("LOAD_ROW " + json.dumps(row, sort_keys=True))

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for _ in range(concurrency):
            pool.submit(worker)
    return sent


def main() -> int:
    """Run the load described by the arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--poll-timeout", type=float, default=240)
    parser.add_argument("--poll-interval", type=float, default=2)
    args = parser.parse_args()
    sent = run_load(http_post(args.url), args.fixture.read_text(encoding="utf-8"), run_id=args.run_id,
                    concurrency=args.concurrency, duration=args.duration, max_requests=args.max_requests,
                    timeout=args.timeout, poll_timeout=args.poll_timeout, poll_interval=args.poll_interval)
    print(f"LOAD_DONE {sent}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
