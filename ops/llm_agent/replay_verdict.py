"""[READ-ONLY] Verdicts for the owner evidence replay (scripts/llm_agent/agent_replay.sh).

The shell helper only collects observations (kubectl/helm JSON, A2A responses, probe evidence, samples); this
module decides PASS/FAIL and sets the exit code, so a failed check can never print a reassuring line and exit 0.
Stdlib only. Nothing here talks to a cluster.

  status     platform readiness from kubectl/helm JSON snapshots (Sandbox-only architecture; retired
             workloads must be absent, and their absence is reported as expected, not as an error)
  e2e        one business request: A2A task state plus the structured Coordinator result for the workflow
  registry   strict Agent Registry readback evidence and the readback exit code
  negative   the three AGI-08 negatives: each evidence file proves its denial with a working control
  keda       a bounded KEDA demonstration: per-job E2E verdicts plus the replica timeline of both autoscalers
  workers    shared WorkerPool sessions: session outcomes and actor placement (sharing is not autoscaling)

Exit codes: 0 PASS, 1 FAIL (or NOT_OBSERVED for a demonstration), 2 usage error.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
import sys
from typing import Any

# --- expected runtime (AGENT_PLATFORM_COMPLETION.md §10-13) -------------------------------------------------
HELM_REVISIONS = {"llm-agent": {59: "before the lf-agents cleanup", 60: "after the lf-agents cleanup (LIVE_VERIFICATION_PENDING)"},
                  "llm-agent-mcp": {19: "current"}}
COORDINATOR = "fraud-coordinator"
GATEWAYS = ("a2a-gateway", "mcp-gateway", "agentgateway")
SANDBOX_AGENTS = ("feature-rag-specialist", "drift-specialist")
WORKER_POOL = "business-agents"
MCP_SERVERS = ("feature-rag-mcp", "drift-mcp")
SCALED_OBJECTS = ("ewallet-agents/fraud-coordinator", "ewallet-sandbox/business-agents-workerpool")
# Retired by sp-cleanup (rev 55) and lc-agents (rev 59); ewallet-agents/feature-rag-mcp has had no Deployment since
# long before (its leftover policies are the pending lf-agents cleanup, not a workload).
RETIRED_DEPLOYMENTS = ("ewallet-agents/feature-rag-specialist", "ewallet-agents/drift-specialist",
                       "ewallet-agents/drift-mcp", "ewallet-agents/feature-rag-mcp")
RETIRED_SCALED_OBJECTS = ("ewallet-agents/feature-rag-specialist", "ewallet-agents/drift-specialist")
FLOOR = 2

E2E_CASES = ("fraud", "drift", "combined", "nl-fraud", "nl-drift")
REGISTRY_EXPECTED = {"Agent": 3, "MCPServer": 2, "Prompt": 3}
NEGATIVES = {  # evidence file -> the denial it must prove
    "wrong-server-credential.json": "unauthenticated",
    "ungranted-tool.json": "call-denied",
    "a2a-unrouted.json": "no-route",
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _items(path: Path) -> list[dict[str, Any]]:
    data = _load(path)
    return data.get("items", []) if isinstance(data, dict) else data


def _conditions(obj: dict[str, Any]) -> dict[str, str]:
    return {c.get("type"): c.get("status") for c in (obj.get("status") or {}).get("conditions") or []}


class Report:
    """Ordered PASS/FAIL/INFO lines; FAIL decides the exit code."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, verdict: str, check: str, detail: str = "") -> None:
        self.rows.append((verdict, check, detail))

    def check(self, ok: bool, check: str, detail: str = "") -> None:
        self.add("PASS" if ok else "FAIL", check, detail)

    @property
    def failed(self) -> list[str]:
        return [check for verdict, check, _ in self.rows if verdict == "FAIL"]

    def emit(self, title: str, out: Path | None) -> int:
        print(f"== {title}")
        for verdict, check, detail in self.rows:
            print(f"{verdict:13} {check}" + (f"  ({detail})" if detail else ""))
        verdict = "FAIL" if self.failed else "PASS"
        print(f"verdict: {verdict}" + (f"  failed: {', '.join(self.failed)}" if self.failed else ""))
        if out:
            out.write_text(json.dumps({"verdict": verdict, "rows": [list(r) for r in self.rows]}, indent=2) + "\n",
                           encoding="utf-8")
        return 1 if self.failed else 0


# --- status ---------------------------------------------------------------------------------------------------
def status(snapshot: Path) -> Report:
    """Readiness of the current runtime from the JSON files agent_replay.sh status writes into ``snapshot``."""

    r = Report()
    for release, accepted in HELM_REVISIONS.items():
        history = _items(snapshot / f"helm-{release}.json")
        last = history[-1] if history else {}
        revision, state = last.get("revision"), last.get("status")
        r.check(revision in accepted and state == "deployed", f"Helm {release}",
                f"revision {revision} {state}" + (f": {accepted[revision]}" if revision in accepted else
                                                  f"; expected {sorted(accepted)}"))
    deployments = {f"{d['metadata']['namespace']}/{d['metadata']['name']}": d
                   for name in ("deployments-ewallet-agents.json", "deployments-ewallet-mcp.json")
                   for d in _items(snapshot / name)}

    def ready(key: str, minimum: int) -> tuple[bool, str]:
        d = deployments.get(key)
        if d is None:
            return False, "missing"
        spec = int((d.get("spec") or {}).get("replicas", 0))
        got = int((d.get("status") or {}).get("readyReplicas", 0) or 0)
        return got >= minimum and got == spec, f"ready {got}/{spec}"

    ok, detail = ready(f"ewallet-agents/{COORDINATOR}", FLOOR)
    r.check(ok, f"Coordinator Deployment {COORDINATOR} (KEDA floor {FLOOR})", detail)
    for gateway in GATEWAYS:
        ok, detail = ready(f"ewallet-agents/{gateway}", 1)
        r.check(ok, f"Gateway {gateway}", detail)
    agents = {a["metadata"]["name"]: a for a in _items(snapshot / "agents.json")}
    coordinator = agents.get(COORDINATOR, {})
    r.check(list(agents) == [COORDINATOR] and (coordinator.get("spec") or {}).get("type") == "BYO"
            and _conditions(coordinator).get("Ready") == "True",
            "kagent Agents: only the BYO Coordinator",
            ", ".join(f"{n} type={(a.get('spec') or {}).get('type')} Ready={_conditions(a).get('Ready')}"
                      for n, a in sorted(agents.items())) or "none")
    sandbox = {a["metadata"]["name"]: a for a in _items(snapshot / "sandboxagents.json")}
    for name in SANDBOX_AGENTS:
        conditions = _conditions(sandbox.get(name, {}))
        r.check(name in sandbox and conditions.get("Ready") == "True" and conditions.get("Accepted", "True") == "True",
                f"SandboxAgent {name}", f"conditions {conditions}" if name in sandbox else "missing")
    pools = {p["metadata"]["name"]: p for p in _items(snapshot / "workerpools.json")}
    pool = pools.get(WORKER_POOL)
    if pool is None:
        r.check(False, f"WorkerPool {WORKER_POOL}", "missing")
    else:
        spec, got = (pool.get("spec") or {}).get("replicas"), (pool.get("status") or {}).get("replicas")
        r.check(isinstance(spec, int) and spec >= FLOOR and got == spec, f"WorkerPool {WORKER_POOL} (shared)",
                f"spec/status {spec}/{got}")
    pdbs = {p["metadata"]["name"] for p in _items(snapshot / "pdb-ewallet-mcp.json")}
    for server in MCP_SERVERS:
        key = f"ewallet-mcp/{server}"
        ok, detail = ready(key, 2)
        d = deployments.get(key, {})
        strategy = (d.get("spec") or {}).get("strategy") or {}
        unavailable = (strategy.get("rollingUpdate") or {}).get("maxUnavailable")
        r.check(ok and strategy.get("type") == "RollingUpdate" and str(unavailable) == "0" and server in pdbs,
                f"MCP server {key}", f"{detail}, {strategy.get('type')} maxUnavailable={unavailable}, "
                                     f"PDB={'yes' if server in pdbs else 'no'}")
    scaled = {f"{s['metadata']['namespace']}/{s['metadata']['name']}": s
              for name in ("scaledobjects-ewallet-agents.json", "scaledobjects-ewallet-sandbox.json")
              for s in _items(snapshot / name)}
    hpas = {f"{h['metadata']['namespace']}/{h['metadata']['name']}": h
            for name in ("hpa-ewallet-agents.json", "hpa-ewallet-sandbox.json") for h in _items(snapshot / name)}
    for key in SCALED_OBJECTS:
        s = scaled.get(key)
        if s is None:
            r.check(False, f"KEDA ScaledObject {key}", "missing")
            continue
        c = _conditions(s)
        annotations = (s.get("metadata") or {}).get("annotations") or {}
        paused = "autoscaling.keda.sh/paused-replicas" in annotations or c.get("Paused") == "True"
        hpa = hpas.get(f"{key.split('/')[0]}/{(s.get('status') or {}).get('hpaName', '')}", {})
        hstat = hpa.get("status") or {}
        r.check(c.get("Ready") == "True" and not paused and c.get("Fallback") != "True",
                f"KEDA ScaledObject {key}",
                f"Ready={c.get('Ready')} Active={c.get('Active')} Paused={paused} Fallback={c.get('Fallback')}; "
                f"min/max {(s.get('spec') or {}).get('minReplicaCount')}/{(s.get('spec') or {}).get('maxReplicaCount')}; "
                f"HPA {hpa.get('metadata', {}).get('name', '?')} current/desired "
                f"{hstat.get('currentReplicas')}/{hstat.get('desiredReplicas')}")
    for key in RETIRED_DEPLOYMENTS:
        r.check(key not in deployments, f"retired Deployment {key} absent",
                "absent (expected)" if key not in deployments else "PRESENT: retired workload is running")
    for key in RETIRED_SCALED_OBJECTS:
        r.check(key not in scaled, f"retired ScaledObject {key} absent",
                "absent (expected)" if key not in scaled else "PRESENT")
    restarted = []
    for name in ("pods-ewallet-agents.json", "pods-ewallet-mcp.json", "pods-ewallet-sandbox.json"):
        for pod in _items(snapshot / name):
            count = sum(int(c.get("restartCount", 0)) for c in (pod.get("status") or {}).get("containerStatuses") or [])
            if count:
                restarted.append(f"{pod['metadata']['namespace']}/{pod['metadata']['name']}={count}")
    r.add("INFO" if not restarted else "WARN", "container restarts", ", ".join(restarted) or "0")
    return r


# --- e2e ------------------------------------------------------------------------------------------------------
def structured_result(task: dict[str, Any]) -> dict[str, Any] | None:
    """The Coordinator's JSON result: the artifact text part that parses to an object with contract_version."""

    for artifact in (task.get("result") or {}).get("artifacts") or []:
        for part in artifact.get("parts") or []:
            try:
                value = json.loads(part.get("text") or "")
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and "contract_version" in value:
                return value
    return None


def _probability(score: Any) -> float | None:
    value = (score or {}).get("fraud_probability") if isinstance(score, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1 else None


def _psi(section: Any) -> float | None:
    drift = (section or {}).get("drift") if isinstance(section, dict) else None
    if not isinstance(drift, list) or not drift or not isinstance(drift[0], dict):
        return None
    value = drift[0].get("psi")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def e2e(case: str, task_path: Path, timing_path: Path, request_id: str | None) -> tuple[str, list[str], dict[str, Any]]:
    """Classify one replay request: COMPLETE, FAILED_TASK, PARTIAL, MISSING_SPECIALIST, BACKEND_UNAVAILABLE or
    INVALID_RESPONSE. Only COMPLETE passes; HTTP 200 or state=completed alone is never enough."""

    summary: dict[str, Any] = {"case": case}
    try:
        task = _load(task_path)
    except (OSError, ValueError) as exc:
        return "INVALID_RESPONSE", [f"no readable A2A response: {exc}"], summary
    try:
        summary["elapsed_seconds"] = _load(timing_path).get("elapsed_seconds")
    except (OSError, ValueError):
        summary["elapsed_seconds"] = None
    if not isinstance(task, dict) or "error" in task or not isinstance(task.get("result"), dict):
        return "FAILED_TASK", [f"A2A error or no result: {json.dumps(task.get('error') if isinstance(task, dict) else task)[:300]}"], summary
    state = ((task["result"].get("status") or {}).get("state"))
    summary["state"] = state
    if state != "completed":
        return "FAILED_TASK", [f"task state {state} (expected completed)"], summary
    result = structured_result(task)
    if result is None:
        return "INVALID_RESPONSE", ["no structured Coordinator result (contract_version) in the artifacts"], summary
    summary.update({k: result.get(k) for k in ("contract_version", "status", "workflow", "unavailable", "request_id")})
    reasons: list[str] = []
    typed = case in ("fraud", "drift", "combined")
    if typed and request_id and result.get("request_id") != request_id:
        reasons.append(f"request_id {result.get('request_id')} is not this request's {request_id}")
    unavailable = result.get("unavailable")
    status_value = result.get("status")
    if case in ("fraud", "nl-fraud"):
        if result.get("contract_version") != "g3-orchestration/v1":
            return "INVALID_RESPONSE", [f"contract {result.get('contract_version')} (expected g3-orchestration/v1)"], summary
        probability = _probability(result.get("fraud_score"))
        summary["fraud_probability"] = probability
        if status_value == "unavailable":
            return "BACKEND_UNAVAILABLE", [f"status unavailable; unavailable={unavailable}"], summary
        if status_value == "partial" or unavailable:
            return "PARTIAL", [f"status {status_value}; unavailable={unavailable}"], summary
        if status_value != "ok":
            return "INVALID_RESPONSE", [f"status {status_value} (expected ok)"], summary
        if probability is None:
            reasons.append("fraud_score.fraud_probability missing or outside [0, 1]")
        if not isinstance(result.get("features"), dict) or not result["features"]:
            reasons.append("no features")
        if unavailable != []:
            reasons.append(f"unavailable={unavailable} (expected [])")
    else:
        expected_workflow = "combined" if case == "combined" else "drift"
        if result.get("contract_version") != "g3-orchestration/v2":
            return "INVALID_RESPONSE", [f"contract {result.get('contract_version')} (expected g3-orchestration/v2)"], summary
        if result.get("workflow") != expected_workflow:
            return "INVALID_RESPONSE", [f"workflow {result.get('workflow')} (expected {expected_workflow})"], summary
        specialists = result.get("specialists") if isinstance(result.get("specialists"), dict) else {}
        needed = ("feature-rag-specialist", "drift-specialist") if case == "combined" else ("drift-specialist",)
        missing = [name for name in needed if not isinstance(specialists.get(name), dict)]
        summary["specialists"] = {name: (s or {}).get("status") for name, s in specialists.items()}
        if missing:
            return "MISSING_SPECIALIST", [f"no result section for {missing}"], summary
        if status_value == "unavailable":
            return "BACKEND_UNAVAILABLE", [f"status unavailable; unavailable={unavailable}"], summary
        not_ok = {name: specialists[name].get("status") for name in needed if specialists[name].get("status") != "ok"}
        if status_value == "partial" or not_ok or unavailable:
            return "PARTIAL", [f"status {status_value}; specialists not ok {not_ok}; unavailable={unavailable}"], summary
        if status_value != "complete":
            return "INVALID_RESPONSE", [f"status {status_value} (expected complete)"], summary
        psi = _psi(specialists["drift-specialist"])
        summary["psi"] = psi
        if psi is None:
            reasons.append("drift-specialist returned no PSI")
        if case == "combined":
            probability = _probability(specialists["feature-rag-specialist"].get("fraud_score"))
            summary["fraud_probability"] = probability
            if probability is None:
                reasons.append("feature-rag-specialist fraud_score.fraud_probability missing")
        if unavailable != []:
            reasons.append(f"unavailable={unavailable} (expected [])")
    return ("INVALID_RESPONSE" if reasons else "COMPLETE"), reasons, summary


# --- registry -------------------------------------------------------------------------------------------------
def registry(readback_path: Path, exit_code: int) -> Report:
    r = Report()
    r.check(exit_code == 0, "registry_catalog readback exit code", str(exit_code))
    try:
        report = _load(readback_path)
        entries = report["entries"]
        assert isinstance(entries, list)
    except (OSError, ValueError, KeyError, AssertionError, TypeError) as exc:
        r.check(False, "readback evidence", f"missing or malformed: {exc}")
        return r
    r.check(report.get("verdict") == "PASS", "strict readback verdict", str(report.get("verdict")))
    counts = collections.Counter(e.get("kind") for e in entries)
    r.check(dict(counts) == REGISTRY_EXPECTED, "catalog entries (3 Agents, 2 MCPServers, 3 Prompts)", str(dict(counts)))
    for e in entries:
        state = "matches" if e.get("matches") else ("NOT REGISTERED" if not e.get("registered") else
                                                    "; ".join((e.get("differences") or [])[:3]))
        r.check(bool(e.get("matches")), f"{e.get('kind', '?'):9} {e.get('name', '?')} {e.get('tag', '')}", state)
    return r


# --- negatives ------------------------------------------------------------------------------------------------
def negative(directory: Path, exit_codes: dict[str, int]) -> Report:
    r = Report()
    for name, expect in NEGATIVES.items():
        code = exit_codes.get(name)
        try:
            evidence = _load(directory / name)
        except (OSError, ValueError) as exc:
            r.check(False, f"{name}: expected {expect}", f"evidence missing or malformed: {exc}")
            continue
        control = evidence.get("control") or {}
        control_ok = (control.get("http_status") == 200) if expect == "no-route" else control.get("outcome") == "listed"
        r.check(code == 0 and evidence.get("verdict") == "PASS" and evidence.get("expect") == expect and control_ok
                and not evidence.get("reasons"),
                f"{name}: expected {expect}",
                f"probe exit {code}, verdict {evidence.get('verdict')}, observed "
                f"{evidence.get('outcome') or (evidence.get('tested') or {}).get('http_status')}, control "
                f"{control.get('outcome') or control.get('http_status')}")
    return r


# --- keda demonstration ---------------------------------------------------------------------------------------
def keda(timeline: Path, verdicts: list[Path]) -> Report:
    """Scale-out and scale-in are claimed only from observed replica counts, never from Ready conditions."""

    r = Report()
    samples = []
    for line in timeline.read_text(encoding="utf-8").splitlines():
        fields = dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
        try:
            samples.append((int(fields["coord"]), int(fields["pool"])))
        except (KeyError, ValueError):
            continue
    r.check(bool(samples), "replica timeline samples", str(len(samples)))
    for index, label in ((0, "Coordinator (ewallet-agents/fraud-coordinator)"), (1, "WorkerPool (ewallet-sandbox/business-agents)")):
        series = [s[index] for s in samples]
        if not series:
            continue
        peak = max(series)
        r.add("PASS" if peak > FLOOR else "NOT_OBSERVED", f"{label} scale-out", f"peak {peak}; series {_compact(series)}")
        if peak > FLOOR:
            r.add("PASS" if series[-1] == FLOOR else "NOT_OBSERVED", f"{label} scale-in to {FLOOR}", f"last {series[-1]}")
    ok = 0
    for path in verdicts:
        try:
            v = _load(path)
            ok += v.get("verdict") == "COMPLETE"
        except (OSError, ValueError):
            pass
    r.check(bool(verdicts) and ok == len(verdicts), "business requests under load", f"{ok}/{len(verdicts)} COMPLETE")
    if any(row[0] == "NOT_OBSERVED" for row in r.rows):
        r.add("FAIL", "autoscaling demonstration", "not observed in this run; no scale claim is made (see the runbook)")
    return r


def _compact(series: list[int]) -> str:
    out: list[str] = []
    for value in series:
        if not out or out[-1].split("x")[0] != str(value):
            out.append(f"{value}x1")
        else:
            n = int(out[-1].split("x")[1]) + 1
            out[-1] = f"{value}x{n}"
    return " ".join(out)


# --- shared workers -------------------------------------------------------------------------------------------
def workers(status_rows: Path, golden: Path, sessions: Path) -> Report:
    r = Report()
    rows = []
    for line in sessions.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "ok" in row:
            rows.append(row)
    ok = sum(1 for row in rows if row.get("ok") is True)
    r.check(bool(rows) and ok == len(rows), "Sandbox session turns executed", f"{ok}/{len(rows)} ok")
    ignore = {line.strip() for line in golden.read_text(encoding="utf-8").splitlines() if line.strip()}
    placement: dict[str, dict[str, set[str]]] = collections.defaultdict(lambda: collections.defaultdict(set))
    for line in status_rows.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        for agent, actors in (row.get("actors") or {}).items():
            for actor in actors or []:
                if actor.get("actor") not in ignore and actor.get("pod"):
                    placement[actor["pod"]][agent].add(actor["actor"])
    for pod, agents in sorted(placement.items()):
        r.add("INFO", f"worker {pod}", ", ".join(f"{a}={len(ids)} actors" for a, ids in sorted(agents.items())))
    shared = [pod for pod, agents in placement.items() if len(agents) >= 2]
    r.check(bool(placement), "actors placed on WorkerPool pods", f"{len(placement)} worker pod(s)")
    r.add("PASS" if shared else "NOT_OBSERVED", "one worker hosted actors of both Specialists (sharing)",
          ", ".join(sorted(shared)) or "placement varies; run again")
    return r


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status"); p.add_argument("--snapshot", type=Path, required=True); p.add_argument("--out", type=Path)
    p = sub.add_parser("e2e")
    p.add_argument("--case", choices=E2E_CASES, required=True)
    p.add_argument("--task", type=Path, required=True)
    p.add_argument("--timing", type=Path, required=True)
    p.add_argument("--request-id")
    p.add_argument("--out", type=Path)
    p = sub.add_parser("registry"); p.add_argument("--readback", type=Path, required=True)
    p.add_argument("--exit-code", type=int, required=True); p.add_argument("--out", type=Path)
    p = sub.add_parser("negative"); p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--exit-code", action="append", default=[], metavar="FILE=CODE"); p.add_argument("--out", type=Path)
    p = sub.add_parser("keda"); p.add_argument("--timeline", type=Path, required=True)
    p.add_argument("--verdict", type=Path, action="append", default=[]); p.add_argument("--out", type=Path)
    p = sub.add_parser("workers"); p.add_argument("--status-rows", type=Path, required=True)
    p.add_argument("--golden", type=Path, required=True); p.add_argument("--sessions", type=Path, required=True)
    p.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.command == "status":
        return status(args.snapshot).emit("platform readiness (Kubernetes readiness only, not business E2E)", args.out)
    if args.command == "e2e":
        verdict, reasons, summary = e2e(args.case, args.task, args.timing, args.request_id)
        print(json.dumps({"verdict": verdict, **summary}, ensure_ascii=False))
        for reason in reasons:
            print(f"  reason: {reason}")
        if args.out:
            args.out.write_text(json.dumps({"verdict": verdict, "reasons": reasons, **summary}, ensure_ascii=False,
                                           indent=2) + "\n", encoding="utf-8")
        return 0 if verdict == "COMPLETE" else 1
    if args.command == "registry":
        return registry(args.readback, args.exit_code).emit("Agent Registry strict readback (GET only)", args.out)
    if args.command == "negative":
        codes = {}
        for item in args.exit_code:
            name, _, code = item.partition("=")
            codes[name] = int(code)
        return negative(args.dir, codes).emit("AGI-08 security negatives", args.out)
    if args.command == "keda":
        return keda(args.timeline, args.verdict).emit("KEDA demonstration (observed replicas)", args.out)
    return workers(args.status_rows, args.golden, args.sessions).emit("shared WorkerPool sessions", args.out)


if __name__ == "__main__":
    sys.exit(main())
