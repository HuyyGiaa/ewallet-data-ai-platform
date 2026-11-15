"""AGS-04 session driver: bounded A2A sessions against SandboxAgents, both agents in parallel.

For each agent it opens ``--contexts`` new contexts and sends ``--messages`` messages per context
through the kagent controller's SandboxAgent route ``/api/a2a-sandboxes/<ns>/<agent>/``. The first
message of a context creates a session actor from the golden snapshot; later messages resume the
suspended actor. Unlike sandbox_client it does not wait for a successful task first, so it also
runs under S0 placeholder credentials, where tool calls fail by design.

Each task prints one ``LOAD_ROW`` JSON line with agent, context, phase (``new``/``resume``), state
and latency. Run ``ops.experiments.substrate_fleet record`` alongside for the worker assignments.

Resume pacing (kagent 0.10.3 defect, reproduced in docs/workplans/edai2/agent-platform-campaign/
resume-rca/): kagent suspends a session actor asynchronously after each turn, and EnsureSessionActor
treats a SUSPENDING actor as needing no resume, so a follow-up sent during that window waits for
RUNNING until it times out (120 s, "not reachable via atenet-router"). Before each follow-up the
driver therefore waits, bounded, until /api/substrate/status reports the session's actor Suspended.
``--no-settle`` sends immediately (recording the actor status at send): against unmodified kagent it
reproduces the failure; against the PKG-T session-turn patch it must pass. ``--burst N`` adds N
concurrent messages on one context.

    python -m ops.experiments.sandbox_sessions --url http://127.0.0.1:18383 --run-id <id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid
from urllib.request import Request, urlopen
from typing import Callable, Mapping, Sequence

from ops.experiments.agent_load import Post, http_post, run_task

FIXTURES = {"feature-rag-specialist": "configs/experiments/fixtures/agent-load-request.json",
            "drift-specialist": "configs/experiments/fixtures/agent-load-drift-request.json"}
Emit = Callable[[str], None]
_DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
SETTLED = "Suspended"


def session_actor_id(namespace: str, agent: str, session: str) -> str:
    """kagent 0.10.3 SandboxAgentSessionActorID (pkg/sandboxbackend/substrate/agent_actor.go)."""
    prefix = f"asr-{namespace}-{agent}".lower().replace("_", "-")
    if len(prefix) > 63:
        prefix = prefix[:63].rstrip("-")
    raw = f"{prefix}-{session.strip().lower().replace('_', '-')}".lower().replace("_", "-")
    if len(raw) <= 63 and _DNS1123.match(raw):
        return raw
    return "asr-" + hashlib.sha256(f"{namespace}/{agent}/{session}".encode()).hexdigest()[:24]


def status_fetch(base: str, namespace: str) -> Callable[[], Mapping[str, object]]:
    url = f"{base.rstrip('/')}/api/substrate/status?namespace={namespace}"

    def fetch() -> Mapping[str, object]:
        with urlopen(Request(url, headers={"accept": "application/json"}), timeout=15) as response:  # noqa: S310
            return json.load(response)
    return fetch


def wait_settled(fetch: Callable[[], Mapping[str, object]], actor_id: str, *, limit: float,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 interval: float = 0.5) -> tuple[bool, str | None, float]:
    """Wait until the actor is Suspended (its previous turn's suspend finished). Returns
    (settled, last status, seconds waited); never waits past ``limit``."""
    start = clock()
    last = None
    while True:
        try:
            data = fetch().get("data", {})
            last = next((a.get("status") for a in data.get("actors", []) if a.get("actorId") == actor_id), None)
        except Exception:  # noqa: BLE001 - a failed read is retried until the limit
            last = "unreadable"
        if last == SETTLED:
            return True, last, clock() - start
        if clock() - start >= limit:
            return False, last, clock() - start
        sleep(interval)


def sandbox_url(base: str, namespace: str, agent: str) -> str:
    return f"{base.rstrip('/')}/api/a2a-sandboxes/{namespace}/{agent}/"


def recording(post: Post) -> tuple[Post, dict]:
    """Wrap post so the last JSON-RPC error message survives (run_task keeps only the state)."""
    last: dict = {}

    def wrapped(payload, timeout):
        reply = post(payload, timeout)
        error = reply.get("error") if isinstance(reply, dict) else None
        last["error"] = str(error.get("message"))[:400] if isinstance(error, dict) else None
        return reply
    return wrapped, last


def one_turn(post: Post, template: str, task_id: str, context: str, timeout: float) -> dict:
    post, last = recording(post)
    row = run_task(post, template, task_id, timeout=timeout, poll_timeout=timeout, poll_interval=2, context_id=context)
    if row.get("state") == "rpc_error":
        row["rpc_error_message"] = last.get("error")
    return row


def agent_sessions(post: Post, template: str, agent: str, run_id: str, *, contexts: int, messages: int,
                   timeout: float, emit: Emit, lock: threading.Lock, namespace: str = "ewallet-sandbox",
                   settle: Callable[[str], tuple[bool, str | None, float]] | None = None,
                   observe: Callable[[str], str | None] | None = None, context_id: str | None = None,
                   burst: int = 0) -> None:
    def emit_row(row: dict) -> None:
        with lock:
            emit("LOAD_ROW " + json.dumps(row, sort_keys=True))

    for c in range(contexts):
        context = context_id or f"{run_id}-{agent}-c{c}-{uuid.uuid4().hex[:6]}"
        actor = session_actor_id(namespace, agent, context)
        base = {"agent": agent, "context": context, "actor": actor}
        stopped = False
        for m in range(messages):
            extra: dict[str, object] = {}
            if m > 0 and settle is not None:
                settled, status, waited = settle(actor)
                extra = {"settled_before_send": settled, "status_before_send": status, "settle_s": round(waited, 2)}
                if not settled:
                    emit_row({"id": f"{context}-m{m}", "state": "not_sent", "ok": False, "phase": "resume", **base, **extra,
                              "error": "previous suspend did not settle; follow-up not sent (would hit the kagent race)"})
                    stopped = True
                    break
            elif m > 0 and observe is not None:
                extra = {"status_at_send": observe(actor)}  # one read, no waiting: the server must cope
            row = one_turn(post, template, f"{context}-m{m}", context, timeout)
            emit_row({**row, **base, "phase": "new" if m == 0 else "resume", **extra})
        if burst and not stopped:
            # Concurrent requests on one contextId: the runtime must serialize them on one actor.
            status = observe(actor) if observe is not None else None
            threads = [threading.Thread(target=lambda b=b: emit_row({
                **one_turn(post, template, f"{context}-b{b}", context, timeout), **base, "phase": "burst",
                "status_at_send": status})) for b in range(burst)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()


def run(base: str, namespace: str, run_id: str, *, agents: Sequence[str], contexts: int, messages: int, timeout: float,
        root: Path, emit: Emit, make_post: Callable[[str, Mapping[str, str]], Post] = http_post,
        settle: Callable[[str], tuple[bool, str | None, float]] | None = None,
        observe: Callable[[str], str | None] | None = None, context_id: str | None = None, burst: int = 0) -> None:
    lock = threading.Lock()
    threads = []
    for agent in agents:
        post = make_post(sandbox_url(base, namespace, agent), {"X-User-Id": f"sessions-{run_id}"})
        template = (root / FIXTURES[agent]).read_text(encoding="utf-8")
        threads.append(threading.Thread(target=agent_sessions, args=(post, template, agent, run_id),
                                        kwargs={"contexts": contexts, "messages": messages, "timeout": timeout,
                                                "emit": emit, "lock": lock, "namespace": namespace, "settle": settle,
                                                "observe": observe, "context_id": context_id, "burst": burst}))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def status_reader(fetch: Callable[[], Mapping[str, object]]) -> Callable[[str], str | None]:
    def read(actor_id: str) -> str | None:
        try:
            data = fetch().get("data", {})
        except Exception:  # noqa: BLE001 - observation only
            return "unreadable"
        return next((a.get("status") for a in data.get("actors", []) if a.get("actorId") == actor_id), "absent")
    return read


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="local port-forward to kagent-controller, http://127.0.0.1:<port>")
    parser.add_argument("--namespace", default="ewallet-sandbox")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--agents", default="feature-rag-specialist,drift-specialist")
    parser.add_argument("--contexts", type=int, default=4)
    parser.add_argument("--messages", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--settle-limit", type=float, default=60, help="max seconds to wait for the previous suspend")
    parser.add_argument("--no-settle", action="store_true",
                        help="send follow-ups at once, recording the actor status at send (the runtime must cope)")
    parser.add_argument("--context-id", help="reuse one known contextId (one agent, one context)")
    parser.add_argument("--burst", type=int, default=0, help="then send N concurrent messages on the same context (0-4)")
    args = parser.parse_args(argv)
    if not args.url.startswith("http://127.0.0.1:"):
        parser.error("local port-forwards only: http://127.0.0.1:<port>")
    if not (1 <= args.contexts <= 10 and 1 <= args.messages <= 5):
        parser.error("bounded: 1-10 contexts and 1-5 messages per agent")
    if not 0 <= args.burst <= 4:
        parser.error("bounded: --burst 0-4")
    agents = [name for name in args.agents.split(",") if name]
    unknown = set(agents) - set(FIXTURES)
    if unknown:
        parser.error(f"unknown agents {sorted(unknown)}")
    if args.context_id and (len(agents) != 1 or args.contexts != 1):
        parser.error("--context-id needs exactly one agent and --contexts 1")
    fetch = status_fetch(args.url, args.namespace)
    settle = observe = None
    if args.no_settle:
        observe = status_reader(fetch)
    else:
        settle = lambda actor: wait_settled(fetch, actor, limit=args.settle_limit)  # noqa: E731
    run(args.url, args.namespace, args.run_id, agents=agents, contexts=args.contexts, messages=args.messages,
        timeout=args.timeout, root=Path(__file__).resolve().parents[2], emit=lambda line: print(line, flush=True),
        settle=settle, observe=observe, context_id=args.context_id, burst=args.burst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
