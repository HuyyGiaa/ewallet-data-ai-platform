"""Record which Substrate worker pods host which agents' actors, to show a shared WorkerPool.

kagent 0.10.3 serves ``GET /api/substrate/status?namespace=<ns>`` with the WorkerPools,
ActorTemplates, actors and workers it sees through ate-api. A SandboxAgent's ActorTemplate is
named ``<agent>`` or ``<agent>-<hex shape hash>``, so each actor maps to exactly one agent.

Each poll is one JSONL row. The summary says a pool is shared only when the run saw a single
worker pod of that pool host actors of two or more different agents (at the same time or one
after another). Two agents merely naming the same pool is not counted as sharing.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.request import Request, urlopen

Fetch = Callable[[], Mapping[str, Any]]
_HASH = re.compile(r"^[0-9a-f]{1,16}$")
_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def agent_for(template: str, agents: Sequence[str]) -> str | None:
    """Return the agent that owns an ActorTemplate name, or None."""

    for agent in agents:
        if template == agent or (template.startswith(agent + "-") and _HASH.match(template[len(agent) + 1:])):
            return agent
    return None


def http_fetch(base_url: str, namespace: str, *, timeout: float = 15) -> Fetch:
    if not _NAMESPACE.match(namespace):
        raise ValueError("namespace must be a DNS label")
    url = base_url.rstrip("/") + f"/api/substrate/status?namespace={namespace}"

    def fetch() -> Mapping[str, Any]:
        with urlopen(Request(url, headers={"accept": "application/json"}), timeout=timeout) as response:  # noqa: S310
            return json.load(response)

    return fetch


def snapshot(status: Mapping[str, Any], agents: Sequence[str]) -> dict[str, Any]:
    """Reduce one status response to the per-agent actors and per-pod assignments."""

    data = status.get("data", status)
    actors: dict[str, list[dict[str, Any]]] = {agent: [] for agent in agents}
    pods: dict[str, set[str]] = {}
    for actor in data.get("actors") or []:
        agent = agent_for(actor.get("actorTemplateName", ""), agents)
        if agent is None:
            continue
        pod = actor.get("ateomPodName") or ""
        actors[agent].append({"actor": actor.get("actorId"), "status": actor.get("status"), "pod": pod,
                              "pool": actor.get("workerPoolName")})
        if pod:
            pods.setdefault(pod, set()).add(agent)
    for worker in data.get("workers") or []:
        agent = agent_for(worker.get("actorTemplate", ""), agents)
        if agent and worker.get("workerPod"):
            pods.setdefault(worker["workerPod"], set()).add(agent)
    return {
        "enabled": bool(data.get("enabled")),
        "ate_api_error": data.get("ateApiError") or None,
        "pools": {p["name"]: p.get("replicas") for p in data.get("workerPools") or []},
        "templates": {t["name"]: t.get("phase") for t in data.get("actorTemplates") or [] if agent_for(t["name"], agents)},
        "worker_pods": sorted(w.get("workerPod") for w in data.get("workers") or [] if w.get("workerPod")),
        "actors": actors,
        "pod_agents": {pod: sorted(names) for pod, names in sorted(pods.items())},
    }


def summarize(rows: Iterable[Mapping[str, Any]], agents: Sequence[str], pool: str) -> dict[str, Any]:
    seen: dict[str, set[str]] = {}
    pools: set[str] = set()
    errors = 0
    polls = 0
    for row in rows:
        polls += 1
        if row.get("collect_error") or row.get("ate_api_error"):
            errors += 1
            continue
        for pod, names in row.get("pod_agents", {}).items():
            seen.setdefault(pod, set()).update(names)
        for entries in row.get("actors", {}).values():
            pools.update(entry["pool"] for entry in entries if entry.get("pool"))
    shared = sorted(pod for pod, names in seen.items() if len(names) >= 2 and pod.startswith(pool + "-"))
    agents_seen = sorted(set().union(*seen.values())) if seen else []
    verdict = ("SHARED_WORKERS_OBSERVED" if shared and pools == {pool}
               else "NOT_OBSERVED" if polls > errors else "NO_DATA")
    return {"verdict": verdict, "pool": pool, "polls": polls, "failed_polls": errors, "actor_pools": sorted(pools),
            "agents_with_actors": agents_seen, "shared_pods": shared,
            "pod_agents": {pod: sorted(names) for pod, names in sorted(seen.items())},
            "missing_agents": sorted(set(agents) - set(agents_seen))}


def poll(fetch: Fetch, agents: Sequence[str], out: Path, *, interval: float, duration: float,
         clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> int:
    deadline = clock() + duration
    count = 0
    with out.open("a", encoding="utf-8") as handle:
        while True:
            try:
                row: dict[str, Any] = snapshot(fetch(), agents)
            except Exception as error:  # noqa: BLE001 - a failed read is a recorded gap, not a crash
                row = {"collect_error": type(error).__name__}
            row["t"] = clock()
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            count += 1
            if clock() >= deadline:
                return count
            sleep(interval)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record")
    record.add_argument("--url", required=True, help="kagent controller base URL (a local port-forward)")
    record.add_argument("--namespace", default="ewallet-sandbox")
    record.add_argument("--agents", default="feature-rag-specialist,drift-specialist")
    record.add_argument("--out", type=Path, required=True)
    record.add_argument("--interval", type=float, default=5)
    record.add_argument("--duration", type=float, default=600)
    report = sub.add_parser("summarize")
    report.add_argument("rows", type=Path)
    report.add_argument("--agents", default="feature-rag-specialist,drift-specialist")
    report.add_argument("--pool", default="business-agents")
    args = parser.parse_args(argv)
    agents = [name.strip() for name in args.agents.split(",") if name.strip()]
    if args.command == "record":
        polls = poll(http_fetch(args.url, args.namespace), agents, args.out, interval=args.interval, duration=args.duration)
        print(json.dumps({"polls": polls, "out": str(args.out)}))
        return 0
    rows = [json.loads(line) for line in args.rows.read_text(encoding="utf-8").splitlines() if line.strip()]
    result = summarize(rows, agents, args.pool)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "SHARED_WORKERS_OBSERVED" else 3


if __name__ == "__main__":
    raise SystemExit(main())
