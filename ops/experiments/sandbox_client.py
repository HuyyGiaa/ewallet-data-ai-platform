"""Drive one EXP-C5-SANDBOX arm through the kagent controller.

Both arms are reached the same way, through the controller's A2A route, so the path is not
a difference between them. Phases, in order:

1. ``first``: retry a new session until one task succeeds, or the limit passes.
2. ``new``: tasks in new sessions. A sandbox starts an actor for each.
3. ``resume``: tasks in one session. A sandbox resumes a suspended actor for each.

Every task is printed as a ``LOAD_ROW`` with its phase, including every failed attempt in
the first phase. With ``--after-failure`` the client only waits for the first success after
an injected failure, for the recovery measurement.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from ops.experiments.agent_load import Post, http_post, run_task

Emit = Callable[[str], None]


def _attempt(post: Post, template: str, phase: str, index: int, run_id: str, context: str, *, timeout: float,
             emit: Emit) -> Mapping[str, Any]:
    row = run_task(post, template, f"{run_id}-{phase}-{index:03d}", timeout=timeout, poll_timeout=timeout,
                   poll_interval=2, context_id=context)
    row["phase"] = phase
    emit("LOAD_ROW " + json.dumps(row, sort_keys=True))
    return row


def wait_for_success(post: Post, template: str, phase: str, run_id: str, *, limit: float, timeout: float, pause: float,
                     emit: Emit, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Try new sessions until one task succeeds; return whether one did within the limit."""

    deadline = time.monotonic() + limit
    index = 0
    while time.monotonic() < deadline:
        index += 1
        if _attempt(post, template, phase, index, run_id, uuid.uuid4().hex, timeout=timeout, emit=emit)["ok"]:
            return True
        sleep(pause)
    return False


def run_phases(post: Post, template: str, run_id: str, *, tasks: int, limit: float, timeout: float, emit: Emit) -> bool:
    """Run all phases; return whether the agent ever answered."""

    if not wait_for_success(post, template, "first", run_id, limit=limit, timeout=timeout, pause=5, emit=emit):
        return False
    for index in range(1, tasks + 1):
        _attempt(post, template, "new", index, run_id, uuid.uuid4().hex, timeout=timeout, emit=emit)
    session = uuid.uuid4().hex
    for index in range(1, tasks + 1):
        _attempt(post, template, "resume", index, run_id, session, timeout=timeout, emit=emit)
    return True


def main() -> int:
    """Run the client against a local port-forward to the controller."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tasks", type=int, default=5)
    parser.add_argument("--limit", type=float, default=600)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--after-failure", action="store_true")
    args = parser.parse_args()
    if not args.url.startswith("http://127.0.0.1:"):
        parser.error("local port-forwards only: http://127.0.0.1:<port>")
    post = http_post(args.url, {"X-User-Id": "exp-c5-sandbox"})
    template = args.fixture.read_text(encoding="utf-8")
    def emit(line: str) -> None:
        print(line, flush=True)

    if args.after_failure:
        ok = wait_for_success(post, template, "recovery", args.run_id, limit=args.limit, timeout=args.timeout, pause=2, emit=emit)
    else:
        ok = run_phases(post, template, args.run_id, tasks=args.tasks, limit=args.limit, timeout=args.timeout, emit=emit)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
