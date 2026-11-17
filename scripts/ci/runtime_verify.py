"""Runtime verification of a production release through the existing owner-replay helper.

The business checks already exist and are verdict-driven: ``scripts/llm_agent/agent_replay.sh``
collects observations and ``ops/llm_agent/replay_verdict.py`` decides PASS/FAIL. This adapter only
maps the release units' ``verify`` and ``prerequisites`` names to those commands, runs each once,
and aggregates the exit codes and verdict files into one release verdict. It never re-implements a
business test, and a missing verdict file is a failure, not a pass.

    platform-status      agent_replay.sh status       (releases, Coordinator, gateways, SandboxAgents,
                                                       WorkerPool, MCP, KEDA, retired workloads absent)
    sandbox-fleet-live   agent_replay.sh status       (same command; a prerequisite, verified not redeployed)
    registry-readback    agent_replay.sh registry
    e2e-fraud|drift|combined  agent_replay.sh e2e <case>
    security-negatives   agent_replay.sh negative

The helper needs its campaign env (``AGI_ENV``), a reachable cluster and its port-forwards
(``agent_replay.sh pf``); in this batch it runs only against fakes (tests/ci/test_runtime_verify.py).

Two CI/CD-side safeguards wrap the frozen helper (which is not changed):

* target: ``guard_target`` refuses to run unless the campaign env's ``CTX`` is exactly the production
  context the release was approved for, so a verification can never silently read another cluster;
* reconciliation: ``replay_verdict.status`` pins the Helm revisions of the last owner campaign
  (llm-agent 59/60, llm-agent-mcp 19). After an approved promotion the revision moves, so a failing
  ``Helm <release>`` row is accepted only when the snapshot shows exactly the revision this
  transaction produced, ``deployed``. Every other failing row still fails. The running images in
  the same snapshot must equal the plan digests (``IDENTITY``); both appear in the verdict as
  separate rows, never folded into the frozen verdict.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/llm_agent/agent_replay.sh"
CHECKS: dict[str, tuple[tuple[str, ...], str]] = {
    # name -> (helper arguments, verdict file glob relative to REPLAY_DIR)
    "platform-status": (("status",), "status/verdict.json"),
    "sandbox-fleet-live": (("status",), "status/verdict.json"),
    "registry-readback": (("registry",), "registry/verdict.json"),
    "e2e-fraud": (("e2e", "fraud"), "e2e/*-verdict.json"),
    "e2e-drift": (("e2e", "drift"), "e2e/*-verdict.json"),
    "e2e-combined": (("e2e", "combined"), "e2e/*-verdict.json"),
    "security-negatives": (("negative",), "negative/verdict.json"),
}
PASSING = {"PASS", "COMPLETE"}
Runner = Callable[[Sequence[str], dict[str, str]], int]


class VerificationError(ValueError):
    """A verification name is unknown, or the verification would read the wrong cluster."""


def campaign_context(env_file: Path) -> str:
    """The ``CTX=`` the owner campaign env sets (agent_replay.sh sources it)."""

    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip().removeprefix("export ").strip()
        if line.startswith("CTX="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def guard_target(environment: Mapping[str, str], production_context: str) -> None:
    env_file = environment.get("AGI_ENV", "")
    if not env_file or not Path(env_file).is_file():
        raise VerificationError("AGI_ENV must name the campaign env file of the production context")
    actual = campaign_context(Path(env_file))
    if not production_context or production_context == "in-cluster" or actual != production_context:
        raise VerificationError(f"TARGET_MISMATCH: the campaign env targets {actual or 'nothing'}, "
                                f"not the approved production context {production_context or '(none)'}")


def _items(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else list(data.get("items") or [])


def _running_images(snapshot: Path, key: str) -> list[str]:
    namespace, kind, name = key.split("/")
    if kind == "SandboxAgent":
        items = {i["metadata"]["name"]: i for i in _items(snapshot / "sandboxagents.json")}
        image = (((items.get(name, {}).get("spec") or {}).get("byo") or {}).get("deployment") or {}).get("image")
        return [image] if image else []
    items = {i["metadata"]["name"]: i for i in _items(snapshot / f"deployments-{namespace}.json")}
    containers = (((items.get(name, {}).get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    return [c.get("image") for c in containers if c.get("image")]


# The only frozen-verdict failure CI/CD may accept: a Helm revision row of replay_verdict.status, whose
# detail it writes as "revision <n> <state>; expected [<campaign revisions>]".
HELM_ROW = re.compile(r"^Helm (llm-agent|llm-agent-mcp)$")
HELM_DETAIL = re.compile(r"^revision ([0-9]+) ([a-z-]+); expected \[[0-9, ]*\]$")
FROZEN_FAILURE_EXIT = 1  # replay_verdict exits 1 when a row fails; any other nonzero code is a command failure


def _frozen_rows(path: Path) -> tuple[str | None, list[tuple[str, str, str]], str | None]:
    """(frozen verdict, rows, problem) of a status verdict file; any malformation is a problem."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None, [], "MISSING_VERDICT: no status/verdict.json"
    except ValueError:
        return None, [], "MALFORMED_VERDICT: status/verdict.json is not JSON"
    if not isinstance(data, dict) or data.get("verdict") not in {"PASS", "FAIL"}:
        return None, [], "MALFORMED_VERDICT: verdict is not PASS or FAIL"
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows or not all(
            isinstance(r, list) and len(r) == 3 and all(isinstance(x, str) for x in r) and r[0] in {"PASS", "FAIL", "INFO"}
            for r in rows):
        return None, [], "MALFORMED_VERDICT: rows are missing or not [verdict, check, detail]"
    failed = any(r[0] == "FAIL" for r in rows)
    if (data["verdict"] == "FAIL") != failed:
        return None, [], "INCONSISTENT_VERDICT: the verdict does not match its rows"
    return data["verdict"], [tuple(r) for r in rows], None  # type: ignore[misc]


def reconcile_status(replay_dir: Path, expected: Mapping[str, Any], exit_code: int) -> dict[str, Any]:
    """Fail-closed decision on the frozen status verdict after a promotion.

    Outcome ``PASS``, ``PASS_WITH_ACCEPTED_EXCEPTIONS`` (each exception listed) or ``FAIL``. A failing
    frozen row is accepted only when it is a ``Helm <release>`` revision row whose frozen detail and the
    snapshot's Helm history both show exactly the revision this transaction recorded, ``deployed``.
    Everything else fails: other failing rows, a missing, malformed or inconsistent verdict, an exit
    code other than 0 (PASS) or 1 (FAIL), no runtime row besides Helm, and any image identity mismatch.
    """

    snapshot = replay_dir / "status"
    rows: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    reasons: list[str] = []
    frozen, frozen_rows, problem = _frozen_rows(snapshot / "verdict.json")
    if problem:
        reasons.append(problem)
    elif exit_code != (0 if frozen == "PASS" else FROZEN_FAILURE_EXIT):
        reasons.append(f"UNEXPECTED_COMMAND_FAILURE: exit {exit_code} with frozen verdict {frozen}")
    revisions = expected.get("helm_revisions") or {}
    meaningful = [r for r in frozen_rows if r[0] == "PASS" and not HELM_ROW.fullmatch(r[1])]
    if frozen_rows and not meaningful:
        reasons.append("NO_RUNTIME_ROWS: the frozen verdict has no passing runtime check besides Helm")
    for verdict, check, detail in frozen_rows:
        if verdict != "FAIL":
            continue
        match, detail_match = HELM_ROW.fullmatch(check), HELM_DETAIL.fullmatch(detail)
        release = match.group(1) if match else None
        if release in revisions and detail_match:
            history = _items(snapshot / f"helm-{release}.json")
            last = history[-1] if history else {}
            ok = (int(detail_match.group(1)) == revisions[release] == last.get("revision")
                  and detail_match.group(2) == "deployed" and last.get("status") == "deployed")
            row = {"check": f"RECONCILED {check}", "status": "PASS" if ok else "FAIL",
                   "detail": f"frozen row: {detail}; snapshot {last.get('revision')} {last.get('status')}; "
                             f"this transaction recorded {revisions[release]}"}
            rows.append(row)
            if ok:
                accepted.append({"kind": "HISTORICAL_HELM_REVISION", "check": check, "frozen_detail": detail,
                                 "accepted_revision": revisions[release]})
            else:
                reasons.append(f"HELM_REVISION_NOT_RECONCILED: {check}")
        else:
            rows.append({"check": check, "status": "FAIL", "detail": detail})
            reasons.append(f"RUNTIME_FAILURE: {check} ({detail[:120]})")
    images = expected.get("images") or {}
    if not images:
        reasons.append("NO_IDENTITY_EXPECTATION: the transaction declared no running image to verify")
    for key, image in sorted(images.items()):
        running = _running_images(snapshot, key)
        ok = bool(running) and all(item == image for item in running)
        rows.append({"check": f"IDENTITY {key}", "status": "PASS" if ok else "FAIL",
                     "detail": "runs the plan digest" if ok else f"runs {running or 'nothing'}"})
        if not ok:
            reasons.append(f"IDENTITY_MISMATCH: {key}")
    status = "FAIL" if reasons else ("PASS_WITH_ACCEPTED_EXCEPTIONS" if accepted else "PASS")
    return {"status": status, "frozen_verdict": frozen, "exit_code": exit_code, "rows": rows,
            "accepted_exceptions": accepted, "reasons": reasons}


@dataclass
class CheckResult:
    name: str
    command: str
    exit_code: int
    verdicts: list[str]
    status: str


def _run(argv: Sequence[str], env: dict[str, str]) -> int:
    return subprocess.run(list(argv), env=env, check=False).returncode


def checks_for(units: Sequence[dict]) -> list[str]:
    """Prerequisites first, then each unit's verifications; each distinct command once, in order."""

    names: list[str] = []
    for unit in units:
        for name in [*unit.get("prerequisites", []), *unit.get("verify", [])]:
            if name in CHECKS and name not in names:
                names.append(name)
    return names


def run_checks(names: Sequence[str], replay_dir: Path, *, helper: Path = HELPER, run: Runner = _run,
               environment: dict[str, str] | None = None, expected: Mapping[str, Any] | None = None,
               production_context: str | None = None) -> dict:
    unknown = [name for name in names if name not in CHECKS]
    if unknown:
        raise VerificationError(f"unknown runtime checks {unknown}")
    env = {**(environment if environment is not None else os.environ), "REPLAY_DIR": str(replay_dir)}
    if production_context is not None:
        guard_target(env, production_context)
    results: list[CheckResult] = []
    reconciliations: list[dict[str, Any]] = []
    done: dict[tuple[str, ...], CheckResult] = {}
    for name in names:
        arguments, pattern = CHECKS[name]
        if arguments in done:  # status serves two names; run it once
            previous = done[arguments]
            results.append(CheckResult(name, previous.command, previous.exit_code, previous.verdicts, previous.status))
            continue
        # Only verdicts this command wrote count: new files, or files it rewrote (status, registry).
        before = {path: path.stat().st_mtime_ns for path in replay_dir.glob(pattern)}
        code = run([str(helper), *arguments], env)
        new = sorted(path for path in replay_dir.glob(pattern)
                     if path not in before or path.stat().st_mtime_ns != before[path])
        verdicts = []
        for path in new:
            try:
                verdicts.append(str(json.loads(path.read_text(encoding="utf-8")).get("verdict")))
            except (OSError, ValueError):
                verdicts.append("UNREADABLE")
        status = "PASS" if code == 0 and verdicts and all(item in PASSING for item in verdicts) else "FAIL"
        if arguments == ("status",) and expected is not None:
            # The reconciliation replaces the plain rule only through its own fail-closed decision; a verdict
            # this command did not (re)write is never reconciled.
            if not new:
                reconciliation = {"status": "FAIL", "reasons": ["MISSING_VERDICT: the status command wrote no verdict"],
                                  "rows": [], "accepted_exceptions": [], "exit_code": code}
            else:
                reconciliation = reconcile_status(replay_dir, expected, code)
            reconciliations.append(reconciliation)
            status = reconciliation["status"]
        result = CheckResult(name, " ".join(arguments), code, verdicts, status)
        done[arguments] = result
        results.append(result)
    passing = {"PASS", "PASS_WITH_ACCEPTED_EXCEPTIONS"}
    verdict = "PASS" if results and all(item.status in passing for item in results) else "FAIL"
    return {"verdict": verdict, "replay_dir": str(replay_dir), "checks": [item.__dict__ for item in results],
            "reconciliation": reconciliations,
            # Visible in the release verdict: every frozen failure that was accepted, and why.
            "accepted_exceptions": [item for r in reconciliations for item in r.get("accepted_exceptions", [])],
            "expected": dict(expected) if expected is not None else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--replay-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--production-context", required=True, help="must equal the campaign env CTX (AGI_ENV)")
    parser.add_argument("--expected", type=Path, help="expected_runtime of the release verdict (reconciliation)")
    args = parser.parse_args(argv)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if plan.get("stage") != "production":
        parser.error("runtime verification runs against production after a promotion")
    args.replay_dir.mkdir(parents=True, exist_ok=True)
    expected = json.loads(args.expected.read_text(encoding="utf-8")) if args.expected else None
    try:
        report = run_checks(checks_for(plan["units"]), args.replay_dir, expected=expected,
                            production_context=args.production_context)
    except VerificationError as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "checks": [(c["name"], c["status"]) for c in report["checks"]]}))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
