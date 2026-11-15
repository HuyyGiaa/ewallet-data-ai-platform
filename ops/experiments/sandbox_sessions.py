"""AGS-04 session driver: bounded A2A sessions against SandboxAgents, both agents in parallel.

For each agent it opens ``--contexts`` new contexts and sends ``--messages`` messages per context
through the kagent controller's SandboxAgent route ``/api/a2a-sandboxes/<ns>/<agent>/``. The first
message of a context creates a session actor from the golden snapshot; later messages resume the
suspended actor. Unlike sandbox_client it does not wait for a successful task first, so it also
runs under S0 placeholder credentials, where tool calls fail by design.

Each task prints one ``LOAD_ROW`` JSON line with agent, context, phase (``new``/``resume``), state
and latency. Run ``ops.experiments.substrate_fleet record`` alongside for the worker assignments.

    python -m ops.experiments.sandbox_sessions --url http://127.0.0.1:18383 --run-id <id>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import threading
import uuid
from typing import Callable, Mapping, Sequence

from ops.experiments.agent_load import Post, http_post, run_task

FIXTURES = {"feature-rag-specialist": "configs/experiments/fixtures/agent-load-request.json",
            "drift-specialist": "configs/experiments/fixtures/agent-load-drift-request.json"}
Emit = Callable[[str], None]


def sandbox_url(base: str, namespace: str, agent: str) -> str:
    return f"{base.rstrip('/')}/api/a2a-sandboxes/{namespace}/{agent}/"


def agent_sessions(post: Post, template: str, agent: str, run_id: str, *, contexts: int, messages: int,
                   timeout: float, emit: Emit, lock: threading.Lock) -> None:
    for c in range(contexts):
        context = f"{run_id}-{agent}-c{c}-{uuid.uuid4().hex[:6]}"
        for m in range(messages):
            row = run_task(post, template, f"{context}-m{m}", timeout=timeout, poll_timeout=timeout, poll_interval=2,
                           context_id=context)
            row.update({"agent": agent, "context": context, "phase": "new" if m == 0 else "resume"})
            with lock:
                emit("LOAD_ROW " + json.dumps(row, sort_keys=True))


def run(base: str, namespace: str, run_id: str, *, agents: Sequence[str], contexts: int, messages: int, timeout: float,
        root: Path, emit: Emit, make_post: Callable[[str, Mapping[str, str]], Post] = http_post) -> None:
    lock = threading.Lock()
    threads = []
    for agent in agents:
        post = make_post(sandbox_url(base, namespace, agent), {"X-User-Id": f"sessions-{run_id}"})
        template = (root / FIXTURES[agent]).read_text(encoding="utf-8")
        threads.append(threading.Thread(target=agent_sessions, args=(post, template, agent, run_id),
                                        kwargs={"contexts": contexts, "messages": messages, "timeout": timeout,
                                                "emit": emit, "lock": lock}))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="local port-forward to kagent-controller, http://127.0.0.1:<port>")
    parser.add_argument("--namespace", default="ewallet-sandbox")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--agents", default="feature-rag-specialist,drift-specialist")
    parser.add_argument("--contexts", type=int, default=4)
    parser.add_argument("--messages", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    if not args.url.startswith("http://127.0.0.1:"):
        parser.error("local port-forwards only: http://127.0.0.1:<port>")
    if not (1 <= args.contexts <= 10 and 1 <= args.messages <= 5):
        parser.error("bounded: 1-10 contexts and 1-5 messages per agent")
    agents = [name for name in args.agents.split(",") if name]
    unknown = set(agents) - set(FIXTURES)
    if unknown:
        parser.error(f"unknown agents {sorted(unknown)}")
    run(args.url, args.namespace, args.run_id, agents=agents, contexts=args.contexts, messages=args.messages,
        timeout=args.timeout, root=Path(__file__).resolve().parents[2], emit=lambda line: print(line, flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
