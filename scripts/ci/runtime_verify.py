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
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Callable, Sequence

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
    """A verification name is unknown."""


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
               environment: dict[str, str] | None = None) -> dict:
    unknown = [name for name in names if name not in CHECKS]
    if unknown:
        raise VerificationError(f"unknown runtime checks {unknown}")
    env = {**(environment if environment is not None else os.environ), "REPLAY_DIR": str(replay_dir)}
    results: list[CheckResult] = []
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
        result = CheckResult(name, " ".join(arguments), code, verdicts, status)
        done[arguments] = result
        results.append(result)
    verdict = "PASS" if results and all(item.status == "PASS" for item in results) else "FAIL"
    return {"verdict": verdict, "replay_dir": str(replay_dir), "checks": [item.__dict__ for item in results]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--replay-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if plan.get("stage") != "production":
        parser.error("runtime verification runs against production after a promotion")
    args.replay_dir.mkdir(parents=True, exist_ok=True)
    report = run_checks(checks_for(plan["units"]), args.replay_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "checks": [(c["name"], c["status"]) for c in report["checks"]]}))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
