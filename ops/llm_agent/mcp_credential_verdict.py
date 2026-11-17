"""Decision logic of the per-replica MCP credential check (scripts/llm_agent/mcp_credential_check.sh).

The shell harness only collects observations into a JSON-lines record file; this module decides. It never
trusts an exit code alone: every required check must be backed by a readable probe evidence file whose raw
observation (stage, tools, outcome, control) proves the expected mechanism.

Each probe is classified on three separate layers, so a backend problem is never read as a credential
result (or the other way round):

  authentication  pass      the server accepted the credential (tools/list answered)
                  rejected  HTTP 401 at initialize
                  unknown   anything else (transport error, 5xx, missing evidence)
  authorization   granted   tools/call reached the tool (any in-band tool status)
                  denied    the server's hidden-tool error for a listed tool
                  n/a       no call was made or it never got that far
  execution       ok        the tool returned status ok
                  backend_unavailable  the tool answered status unavailable (e.g. drift_baseline_unavailable)
                  failed    rejected/timeout/error or no in-band result
                  n/a       no authorized call

Checks per Ready replica of an active server:
  active     the live SandboxAgent credential: authentication pass, authorization granted, and execution ok
             when execution is required (always for the ewallet-mcp servers)
  discovery  the kagent controller (list-only): authentication pass, the tool listed, the call denied
  invalid    a locally generated random credential (never a real one): HTTP 401 at initialize
Negative checks (discovery, invalid) also need the authorized control (the active credential) to list the
tool on the same replica; otherwise a down server or a broken route would look like a denial.

Servers are declared as <namespace>/<name>=<mode>:
  active   every Ready replica runs active + discovery + invalid; replicas must equal the Deployment's
           spec.replicas and its readyReplicas, and be at least --min-replicas
  legacy   (the in-release server before its retirement) active with execution not required + invalid; it
           has no discovery support (at least one replica). A backend_unavailable result is reported as such,
           never as success
  absent   the retired server: its Deployment, Service, PodDisruptionBudget, ServiceAccount and pods are gone

usage: python -m ops.llm_agent.mcp_credential_verdict --records records.jsonl --out verdict.json
           --server ewallet-mcp/feature-rag-mcp=active --server ewallet-mcp/drift-mcp=active
           --server ewallet-agents/drift-mcp=absent [--min-replicas 2]
Exit 0 only when every server passes; 1 on any failure; 2 on usage errors.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import sys
from typing import Any, Iterable

IN_BAND = frozenset({"ok", "rejected", "unavailable", "timeout", "error"})
MODES = ("active", "legacy", "absent")
ABSENT_KINDS = ("deployment", "service", "poddisruptionbudget", "serviceaccount", "pods")
CHECKS = {"active": ("active", "discovery", "invalid"), "legacy": ("active", "invalid")}


@dataclass(frozen=True)
class Layers:
    authentication: str
    authorization: str
    execution: str


@dataclass
class CheckResult:
    server: str
    pod: str
    check: str
    verdict: str
    layers: Layers | None
    reasons: list[str] = field(default_factory=list)


@dataclass
class ServerResult:
    server: str
    mode: str
    verdict: str
    reasons: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)


def classify(evidence: dict[str, Any]) -> Layers:
    """Split one raw probe observation into its authentication, authorization and execution layers."""

    outcome, stage = evidence.get("outcome"), evidence.get("stage")
    if outcome == "unauthenticated" and stage == "initialize":
        return Layers("rejected", "n/a", "n/a")
    if evidence.get("tools") is None:
        return Layers("unknown", "n/a", "n/a")
    if outcome == "hidden_tool":
        return Layers("pass", "denied", "n/a")
    if outcome in IN_BAND:
        execution = {"ok": "ok", "unavailable": "backend_unavailable"}.get(outcome, "failed")
        return Layers("pass", "granted", execution)
    return Layers("pass", "n/a", "n/a")


def _control_lists(evidence: dict[str, Any], tool: str) -> bool:
    control = evidence.get("control") or {}
    return control.get("outcome") == "listed" and tool in (control.get("tools") or [])


def evaluate_probe(check: str, evidence: dict[str, Any] | None, *, tool: str, execution_required: bool) -> tuple[str, Layers | None, list[str]]:
    """Decide one probe from its evidence; missing or malformed evidence always fails."""

    if not isinstance(evidence, dict) or "outcome" not in evidence:
        return "FAIL", None, ["missing or malformed probe evidence"]
    if evidence.get("tool") not in (None, tool):
        return "FAIL", None, [f"evidence is for tool {evidence.get('tool')}, expected {tool}"]
    layers = classify(evidence)
    reasons: list[str] = []
    listed = tool in (evidence.get("tools") or [])
    if check == "active":
        if layers.authentication != "pass":
            reasons.append(f"authentication {layers.authentication} (outcome {evidence.get('outcome')})")
        elif not listed:
            reasons.append("the active credential does not list the tool")
        elif layers.authorization != "granted":
            reasons.append(f"authorization {layers.authorization} (outcome {evidence.get('outcome')})")
        elif execution_required and layers.execution != "ok":
            reasons.append(f"business execution {layers.execution} (tool_error_code {evidence.get('tool_error_code')})")
    elif check in ("discovery", "invalid"):
        if not _control_lists(evidence, tool):
            reasons.append("authorized control unavailable: the active credential does not list the tool on this replica")
        if check == "discovery":
            if layers.authorization == "granted":
                reasons.append("unexpected authorization success: the list-only subject called the tool")
            elif layers.authentication != "pass" or not listed:
                reasons.append(f"discovery subject not authenticated with the tool listed (outcome {evidence.get('outcome')})")
            elif layers.authorization != "denied":
                reasons.append(f"expected the hidden-tool denial, observed {evidence.get('outcome')}")
        else:
            if layers.authentication == "pass":
                reasons.append("unexpected authentication success: an invalid credential was accepted")
            elif layers.authentication != "rejected":
                reasons.append(f"expected HTTP 401 at initialize, observed {evidence.get('outcome')} at {evidence.get('stage')}")
    else:
        reasons.append(f"unknown check {check}")
    return ("FAIL" if reasons else "PASS"), layers, reasons


def _load(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def evaluate_server(server: str, mode: str, records: list[dict[str, Any]], min_replicas: int) -> ServerResult:
    namespace, name = server.split("/", 1)
    mine = [r for r in records if r.get("namespace") == namespace and r.get("server") == name]
    result = ServerResult(server, mode, "PASS")
    if mode == "absent":
        seen = {r.get("kind"): r for r in mine if r.get("type") == "absent"}
        for kind in ABSENT_KINDS:
            record = seen.get(kind)
            if record is None:
                result.reasons.append(f"absence of {kind} not checked")
            elif record.get("present") is not False:
                result.reasons.append(f"{kind} still present: {record.get('names') or '?'}")
        result.verdict = "FAIL" if result.reasons else "PASS"
        return result
    replicas = [r for r in mine if r.get("type") == "replicas"]
    if len(replicas) != 1:
        result.reasons.append("replica inventory missing")
        result.verdict = "FAIL"
        return result
    inventory = replicas[0]
    spec, ready, pods = inventory.get("spec"), inventory.get("ready"), list(inventory.get("pods") or [])
    minimum = max(min_replicas, 1) if mode == "active" else 1   # the legacy in-release server runs one replica
    if not isinstance(spec, int) or spec < minimum:
        result.reasons.append(f"spec.replicas {spec} below the expected minimum {minimum}")
    if ready != spec or len(pods) != spec:
        result.reasons.append(f"expected {spec} Ready replicas, Deployment reports {ready}, found {len(pods)} Ready pods")
    tool = inventory.get("tool") or ""
    forwards = {r.get("pod"): r for r in mine if r.get("type") == "forward"}
    probes = [r for r in mine if r.get("type") == "probe"]
    for pod in pods:
        forward = forwards.get(pod)
        if forward is None or forward.get("ok") is not True:
            result.reasons.append(f"{pod}: no verified port-forward ({(forward or {}).get('reason', 'not attempted')})")
        for check in CHECKS[mode]:
            rows = [r for r in probes if r.get("pod") == pod and r.get("check") == check]
            if len(rows) != 1:
                result.checks.append(CheckResult(server, pod, check, "FAIL", None,
                                                 [f"expected exactly one {check} probe, found {len(rows)}"]))
                continue
            row = rows[0]
            required = mode == "active" and check == "active"
            verdict, layers, reasons = evaluate_probe(check, _load(row.get("evidence")), tool=tool, execution_required=required)
            result.checks.append(CheckResult(server, pod, check, verdict, layers, reasons))
    unexpected = sorted({r.get("pod") for r in probes} - set(pods))
    if unexpected:
        result.reasons.append(f"probes against pods outside the Ready inventory: {unexpected}")
    if result.reasons or any(c.verdict != "PASS" for c in result.checks):
        result.verdict = "FAIL"
    return result


def read_records(path: Path) -> list[dict[str, Any]]:
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError as exc:
                raise ValueError(f"{path}:{number}: malformed record") from exc
    return records


def evaluate(servers: Iterable[tuple[str, str]], records: list[dict[str, Any]], min_replicas: int) -> dict[str, Any]:
    results = [evaluate_server(server, mode, records, min_replicas) for server, mode in servers]
    fatal = [r.get("reason", "?") for r in records if r.get("type") == "fatal"]
    verdict = "PASS" if results and not fatal and all(r.verdict == "PASS" for r in results) else "FAIL"
    return {"verdict": verdict, "fatal": fatal, "min_replicas": min_replicas,
            "servers": [asdict(r) for r in results]}


def _server(value: str) -> tuple[str, str]:
    target, _, mode = value.partition("=")
    if mode not in MODES or target.count("/") != 1 or not all(target.split("/")):
        raise argparse.ArgumentTypeError(f"--server must be <namespace>/<name>=<{'|'.join(MODES)}>, got {value!r}")
    return target, mode


def summary_lines(report: dict[str, Any]) -> list[str]:
    lines = []
    for server in report["servers"]:
        lines.append(f"{server['verdict']}\t{server['server']}\t{server['mode']}\t{'; '.join(server['reasons'])}")
        for check in server["checks"]:
            layers = check["layers"] or {"authentication": "-", "authorization": "-", "execution": "-"}
            lines.append(f"  {check['verdict']}\t{check['pod']}\t{check['check']}\tauthn={layers['authentication']}"
                         f"\tauthz={layers['authorization']}\texec={layers['execution']}\t{'; '.join(check['reasons'])}")
    lines.extend(f"FATAL\t{reason}" for reason in report["fatal"])
    lines.append(f"VERDICT\t{report['verdict']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--server", type=_server, action="append", required=True)
    parser.add_argument("--min-replicas", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        records = read_records(args.records)
    except (OSError, ValueError) as exc:
        print(f"mcp_credential_verdict: FAIL: {exc}", file=sys.stderr)
        return 1
    report = evaluate(args.server, records, args.min_replicas)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("\n".join(summary_lines(report)))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
