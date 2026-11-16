"""P2-A worker lifecycle acceptance on GKE: R1 (replace one idle worker, ateapi up) and R2 (lose one
idle worker while ateapi is down). Observations and assertions are kept apart.

    python -m ops.experiments.worker_lifecycle snapshot --out <dir> --label <name>     [READ-ONLY]
    python -m ops.experiments.worker_lifecycle watch --out <dir> --seconds N           [READ-ONLY]
    python -m ops.experiments.worker_lifecycle idle --out <dir>                        [READ-ONLY]
    python -m ops.experiments.worker_lifecycle r1 --out <dir> --pod P --uid U          [MUTATES: P2A_APPROVAL=R1:<campaign>]
    python -m ops.experiments.worker_lifecycle r2 --out <dir> --pod P --uid U          [MUTATES: P2A_APPROVAL=R2:<campaign>]
    python -m ops.experiments.worker_lifecycle r3 --out <dir> [--busy-timeout S]             [MUTATES: P2A_APPROVAL=R3:<campaign>]
    python -m ops.experiments.worker_lifecycle assert --out <dir> --test r1|r2 --pod P --uid U   [LOCAL]

The registry is read through kagent's ``/api/substrate/status`` (ate-api ListWorkers/ListActors with
kagent's own authorized client); Valkey is never touched. Kubernetes state comes from kubectl.

r1  deletes the approved worker pod gracefully (no --force, no grace override) and records the
    transition until a replacement is Ready and registered.
r2  scales ate-api-server-deployment to 0, waits until no ateapi pod exists (so no informer can see
    the delete), deletes the approved worker pod gracefully, waits until it is gone, then scales
    ateapi back to 1 and records the recovery. Any failure after the scale-down restores ateapi
    first. The outage is bounded by --max-outage.

r3  (B′ active-actor scale-in) waits until a worker is busy (an actor assigned, while the caller runs a
    business request), deletes exactly that pod gracefully (the ReplicaSet's scale-in path) and records
    the drain until the pod is gone and a replacement is registered.

Env: KUBE_CONTEXT, KAGENT_URL (default http://127.0.0.1:18383), P2A_CAMPAIGN.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

SANDBOX_NS = "ewallet-sandbox"
POOL = "business-agents"
ATE_NS = "ate-system"
ATEAPI = "ate-api-server-deployment"
POD_SELECTOR = f"ate.dev/worker-pool={POOL}"


class Refused(RuntimeError):
    """A gate refused the operation; nothing was mutated by this command."""


# ----------------------------------------------------------------------------- observation

def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + f"{time.time() % 1:.3f}"[1:]


class Cluster:
    def __init__(self, context: str, kagent_url: str, runner: Callable[[list[str]], str] | None = None,
                 fetch: Callable[[str], Mapping[str, Any]] | None = None):
        self.context, self.kagent_url = context, kagent_url.rstrip("/")
        self.runner = runner or (lambda cmd: subprocess.run(cmd, check=True, text=True, capture_output=True).stdout)
        self.fetch = fetch or self._fetch

    def kubectl(self, *args: str) -> str:
        return self.runner(["kubectl", "--context", self.context, "--request-timeout=30s", *args])

    def _fetch(self, path: str) -> Mapping[str, Any]:
        with urllib.request.urlopen(self.kagent_url + path, timeout=15) as response:  # noqa: S310 - local port-forward
            return json.load(response)

    def worker_pods(self) -> list[dict[str, Any]]:
        items = json.loads(self.kubectl("-n", SANDBOX_NS, "get", "pods", "-l", POD_SELECTOR, "-o", "json"))["items"]
        pods = []
        for p in items:
            conds = {c["type"]: c["status"] for c in p.get("status", {}).get("conditions", [])}
            pods.append({"name": p["metadata"]["name"], "uid": p["metadata"]["uid"], "node": p["spec"].get("nodeName"),
                         "phase": p["status"].get("phase"), "ready": conds.get("Ready") == "True",
                         "deleting": bool(p["metadata"].get("deletionTimestamp")),
                         "created": p["metadata"].get("creationTimestamp")})
        return sorted(pods, key=lambda x: x["name"])

    def ateapi(self) -> dict[str, Any]:
        d = json.loads(self.kubectl("-n", ATE_NS, "get", "deployment", ATEAPI, "-o", "json"))
        selector = ",".join(f"{k}={v}" for k, v in sorted(d["spec"]["selector"]["matchLabels"].items()))
        pods = json.loads(self.kubectl("-n", ATE_NS, "get", "pods", "-l", selector, "-o", "json"))["items"]
        return {"spec_replicas": d["spec"].get("replicas"), "ready": d.get("status", {}).get("readyReplicas", 0),
                "pods": [{"name": p["metadata"]["name"], "phase": p["status"].get("phase"),
                          "started": p["status"].get("startTime"), "deleting": bool(p["metadata"].get("deletionTimestamp"))}
                         for p in pods]}

    def registry(self) -> dict[str, Any]:
        try:
            data = self.fetch(f"/api/substrate/status?namespace={SANDBOX_NS}").get("data") or {}
        except Exception as exc:  # noqa: BLE001 - recorded as an observation (ateapi down during R2)
            return {"error": f"{type(exc).__name__}: {exc}"[:200]}
        # kagent 0.10.3 SubstrateWorkerEntry: an assigned worker carries actorId (go/api/httpapi/substrate.go)
        workers = [{"pod": w.get("workerPod"), "pool": w.get("workerPool"), "ip": w.get("ip"),
                    "assigned": bool(w.get("actorId")), "actor": w.get("actorId")}
                   for w in data.get("workers") or [] if w.get("workerNamespace") == SANDBOX_NS]
        actors = [{"id": a.get("actorId"), "atespace": a.get("atespace"), "status": a.get("status"),
                   "pod": a.get("ateomPodName")} for a in data.get("actors") or []]
        return {"workers": sorted(workers, key=lambda w: w["pod"] or ""), "actors": actors}

    def snapshot(self, label: str) -> dict[str, Any]:
        return {"label": label, "time": now(), "pods": self.worker_pods(), "ateapi": self.ateapi(), "registry": self.registry()}


# ----------------------------------------------------------------------------- analysis (pure)

def consistency(snap: Mapping[str, Any]) -> dict[str, Any]:
    """Registry vs Kubernetes for the pool. Observation-derived, no judgement."""
    reg = snap.get("registry") or {}
    if "error" in reg:
        return {"registry_readable": False}
    live = {p["name"] for p in snap["pods"] if not p["deleting"]}
    ready = {p["name"] for p in snap["pods"] if p["ready"] and not p["deleting"]}
    registered = {w["pod"] for w in reg["workers"] if w["pool"] == POOL}
    actor_pods = {a["pod"] for a in reg["actors"] if a.get("pod") and a.get("status") != "Suspended"}
    return {"registry_readable": True,
            "stale_records": sorted(registered - live),
            "unregistered_ready_pods": sorted(ready - registered),
            "actors_on_missing_pods": sorted(p for p in actor_pods if p.startswith(POOL) and p not in live),
            "busy_workers": sorted(w["pod"] for w in reg["workers"] if w["assigned"])}


def idle_candidates(snap: Mapping[str, Any]) -> list[str]:
    """Ready, not deleting, registered, unassigned, and no non-suspended actor on it."""
    c = consistency(snap)
    if not c.get("registry_readable"):
        return []
    reg = snap["registry"]
    registered_free = {w["pod"] for w in reg["workers"] if not w["assigned"]}
    active = {a["pod"] for a in reg["actors"] if a.get("pod") and a.get("status") != "Suspended"}
    return sorted(p["name"] for p in snap["pods"]
                  if p["ready"] and not p["deleting"] and p["name"] in registered_free and p["name"] not in active)


def assess(test: str, timeline: list[Mapping[str, Any]], pod: str, uid: str) -> dict[str, Any]:
    """Assertions over the recorded timeline (snapshots in time order)."""
    first, last = timeline[0], timeline[-1]
    out: dict[str, Any] = {"test": test, "target_pod": pod, "target_uid": uid}
    out["target_existed_before"] = any(p["name"] == pod and p["uid"] == uid for p in first["pods"])
    out["target_gone_at_end"] = all(p["name"] != pod for p in last["pods"])
    before = {p["name"] for p in first["pods"]}
    repl = [p for p in last["pods"] if p["name"] not in before and p["ready"]]
    out["replacement"] = [{"name": p["name"], "uid": p["uid"], "node": p["node"]} for p in repl]
    out["replacement_distinct_uid"] = bool(repl) and all(p["uid"] != uid for p in repl)
    end = consistency(last)
    out["end_consistency"] = end
    out["registry_converged"] = end.get("registry_readable") and not end["stale_records"] and not end["unregistered_ready_pods"]
    out["no_actor_on_missing_pod_at_end"] = end.get("registry_readable") and not end["actors_on_missing_pods"]
    stale_seen = [s["time"] for s in timeline if pod in consistency(s).get("stale_records", [])]
    out["stale_record_window"] = {"first": stale_seen[0], "last": stale_seen[-1], "samples": len(stale_seen)} if stale_seen else None
    reg_times = [s["time"] for s in timeline if any(w["pod"] in {r["name"] for r in repl} for w in (s.get("registry") or {}).get("workers", []))]
    out["replacement_registered_at"] = reg_times[0] if reg_times else None
    if test == "r2":
        down = [s for s in timeline if not s["ateapi"]["pods"]]
        out["ateapi_had_no_pod"] = bool(down)
        out["target_deleted_while_ateapi_down"] = any(all(p["name"] != pod for p in s["pods"]) for s in down)
    out["verdict_inputs_ok"] = all([out["target_existed_before"], out["target_gone_at_end"], out["replacement_distinct_uid"],
                                    out["registry_converged"], out["no_actor_on_missing_pod_at_end"]]
                                   + ([out["ateapi_had_no_pod"], out["target_deleted_while_ateapi_down"]] if test == "r2" else []))
    return out


# ----------------------------------------------------------------------------- mutations

def approval(test: str) -> None:
    campaign = os.environ.get("P2A_CAMPAIGN", "")
    if not campaign or os.environ.get("P2A_APPROVAL") != f"{test.upper()}:{campaign}":
        raise Refused(f"set P2A_APPROVAL={test.upper()}:<campaign> (P2A_CAMPAIGN) only under the owner's {test.upper()} approval")


def precheck(cluster: Cluster, pod: str, uid: str) -> dict[str, Any]:
    snap = cluster.snapshot("precheck")
    if not any(p["name"] == pod and p["uid"] == uid for p in snap["pods"]):
        raise Refused(f"{pod} with uid {uid} is not a live worker pod")
    if pod not in idle_candidates(snap):
        raise Refused(f"{pod} is not idle (assigned, an active actor on it, not Ready, or not registered)")
    if len([p for p in snap["pods"] if p["ready"] and not p["deleting"]]) < 2:
        raise Refused("the pool must have 2 Ready workers before one is replaced")
    return snap


def record(cluster: Cluster, out: Path, label: str, timeline: list) -> dict[str, Any]:
    snap = cluster.snapshot(label)
    timeline.append(snap)
    with (out / "timeline.jsonl").open("a") as f:
        f.write(json.dumps(snap, sort_keys=True) + "\n")
    return snap


def wait_until(cluster: Cluster, out: Path, label: str, timeline: list, cond, timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond(record(cluster, out, label, timeline)):
            return True
        time.sleep(interval)
    return False


def event(out: Path, text: str) -> None:
    with (out / "events.txt").open("a") as f:
        f.write(f"{now()} {text}\n")


def recovered(pod: str):
    def cond(s):
        c = consistency(s)
        return (all(p["name"] != pod for p in s["pods"]) and len([p for p in s["pods"] if p["ready"]]) >= 2
                and c.get("registry_readable") and not c["stale_records"] and not c["unregistered_ready_pods"])
    return cond


def run_r1(cluster: Cluster, out: Path, pod: str, uid: str, timeout: float) -> int:
    approval("r1")
    timeline: list = []
    timeline.append(precheck(cluster, pod, uid))
    event(out, f"R1 delete pod {SANDBOX_NS}/{pod} uid={uid} (graceful)")
    cluster.kubectl("-n", SANDBOX_NS, "delete", "pod", pod, "--wait=false")
    ok = wait_until(cluster, out, "r1", timeline, recovered(pod), timeout)
    event(out, f"R1 recovered={ok}")
    (out / "assessment.json").write_text(json.dumps(assess("r1", timeline, pod, uid), indent=2))
    return 0 if ok else 1


def run_r2(cluster: Cluster, out: Path, pod: str, uid: str, max_outage: float, timeout: float) -> int:
    approval("r2")
    timeline: list = []
    timeline.append(precheck(cluster, pod, uid))
    scaled_down = False
    try:
        event(out, f"R2 scale {ATE_NS}/{ATEAPI} to 0")
        cluster.kubectl("-n", ATE_NS, "scale", "deployment", ATEAPI, "--replicas=0")
        scaled_down, start = True, time.monotonic()
        if not wait_until(cluster, out, "r2-ateapi-down", timeline, lambda s: not s["ateapi"]["pods"], min(120, max_outage)):
            raise RuntimeError("ateapi pod did not terminate")
        event(out, f"R2 ateapi has no pod; delete worker pod {SANDBOX_NS}/{pod} uid={uid} (graceful)")
        cluster.kubectl("-n", SANDBOX_NS, "delete", "pod", pod, "--wait=false")
        left = max_outage - (time.monotonic() - start)
        if not wait_until(cluster, out, "r2-worker-gone", timeline, lambda s: all(p["name"] != pod for p in s["pods"]), left):
            raise RuntimeError("worker pod still present at the outage limit")
        event(out, "R2 worker pod gone while ateapi down")
    finally:
        if scaled_down:
            event(out, f"R2 restore {ATE_NS}/{ATEAPI} to 1")
            cluster.kubectl("-n", ATE_NS, "scale", "deployment", ATEAPI, "--replicas=1")
    ok = wait_until(cluster, out, "r2-recovery", timeline,
                    lambda s: s["ateapi"]["ready"] >= 1 and recovered(pod)(s), timeout)
    event(out, f"R2 recovered={ok}")
    (out / "assessment.json").write_text(json.dumps(assess("r2", timeline, pod, uid), indent=2))
    return 0 if ok else 1


def assess_drain(timeline: list[Mapping[str, Any]], pod: str, uid: str) -> dict[str, Any]:
    """R3: the busy worker kept its actor while terminating and left only after the drain."""
    out: dict[str, Any] = {"test": "r3", "target_pod": pod, "target_uid": uid}
    deleting = [s for s in timeline if any(p["name"] == pod and p["deleting"] for p in s["pods"])]
    out["observed_terminating_samples"] = len(deleting)
    held = [s for s in deleting if any(w["pod"] == pod and w["assigned"] for w in (s.get("registry") or {}).get("workers", []))]
    out["actor_kept_while_terminating_samples"] = len(held)
    out["first_terminating"] = deleting[0]["time"] if deleting else None
    gone = [s["time"] for s in timeline if all(p["name"] != pod for p in s["pods"])]
    out["pod_gone_at"] = gone[0] if gone else None
    ends = consistency(timeline[-1])
    out["end_consistency"] = ends
    out["registry_converged"] = bool(ends.get("registry_readable")) and not ends["stale_records"] and not ends["unregistered_ready_pods"]
    out["verdict_inputs_ok"] = bool(deleting) and bool(gone) and out["registry_converged"]
    return out


def run_r3(cluster: Cluster, out: Path, busy_timeout: float, timeout: float) -> int:
    approval("r3")
    timeline: list = []
    deadline = time.monotonic() + busy_timeout
    target = None
    while time.monotonic() < deadline and target is None:
        snap = record(cluster, out, "r3-wait-busy", timeline)
        live = {p["name"]: p for p in snap["pods"] if not p["deleting"]}
        busy = [w["pod"] for w in (snap.get("registry") or {}).get("workers", []) if w["assigned"] and w["pod"] in live]
        if busy:
            target = live[sorted(busy)[0]]
        else:
            time.sleep(0.5)
    if target is None:
        raise Refused("no worker became busy within the wait; nothing was deleted")
    event(out, f"R3 delete busy worker {SANDBOX_NS}/{target['name']} uid={target['uid']} (graceful, scale-in path)")
    cluster.kubectl("-n", SANDBOX_NS, "delete", "pod", target["name"], "--wait=false")
    ok = wait_until(cluster, out, "r3-drain", timeline,
                    lambda s: all(p["name"] != target["name"] for p in s["pods"]) and consistency(s).get("registry_readable")
                    and not consistency(s)["stale_records"] and not consistency(s)["unregistered_ready_pods"], timeout, interval=0.5)
    event(out, f"R3 drained-and-replaced={ok}")
    (out / "assessment.json").write_text(json.dumps(assess_drain(timeline, target["name"], target["uid"]), indent=2))
    return 0 if ok else 1


# ----------------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["snapshot", "watch", "idle", "r1", "r2", "r3", "assert"])
    parser.add_argument("--busy-timeout", type=float, default=180)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--label", default="snapshot")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--pod")
    parser.add_argument("--uid")
    parser.add_argument("--test", choices=["r1", "r2"])
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--max-outage", type=float, default=300)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.command == "assert":
        timeline = [json.loads(line) for line in (args.out / "timeline.jsonl").read_text().splitlines() if line]
        result = assess(args.test, timeline, args.pod, args.uid)
        print(json.dumps(result, indent=2))
        return 0 if result["verdict_inputs_ok"] else 1
    context = os.environ.get("KUBE_CONTEXT") or sys.exit("KUBE_CONTEXT is required")
    cluster = Cluster(context, os.environ.get("KAGENT_URL", "http://127.0.0.1:18383"))
    try:
        if args.command == "snapshot":
            snap = cluster.snapshot(args.label)
            (args.out / f"snapshot-{args.label}.json").write_text(json.dumps(snap, indent=2, sort_keys=True))
            print(json.dumps({"consistency": consistency(snap), "idle": idle_candidates(snap)}, indent=2))
        elif args.command == "idle":
            snap = cluster.snapshot("idle")
            print(json.dumps({"idle": idle_candidates(snap), "pods": snap["pods"], "consistency": consistency(snap)}, indent=2))
        elif args.command == "watch":
            timeline: list = []
            end = time.monotonic() + args.seconds
            while time.monotonic() < end:
                record(cluster, args.out, "watch", timeline)
                time.sleep(1)
        elif args.command == "r1":
            return run_r1(cluster, args.out, args.pod, args.uid, args.timeout)
        elif args.command == "r2":
            return run_r2(cluster, args.out, args.pod, args.uid, args.max_outage, args.timeout)
        elif args.command == "r3":
            return run_r3(cluster, args.out, args.busy_timeout, args.timeout)
    except Refused as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
