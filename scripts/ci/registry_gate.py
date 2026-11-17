"""Read-only Prompt Management gate for the ``registry-gate`` release unit (rubric row 34).

Checks, in order, each fail-closed:

1. ``git-manifest``: every prompt pinned in ``infra/helm/llm-agent-platform/prompts/manifest.json``
   exists and its bytes hash to the pinned sha256 (offline);
2. ``registry``: ``ops/llm_agent/prompt_registry.py verify-all`` (the existing tool) confirms every
   pinned version is byte-identical in agentregistry. A missing registry URL is a failure, never a
   skipped check.

It publishes nothing. Its verdict also states, as a fixed fact of the frozen application, that
registry consistency is not runtime consumption: the BYO Coordinator sends a system prompt fixed in
``ai_platform/llm_agent/nl_intake.py`` and the SandboxAgent Specialists are typed-only, so no running
agent reads the managed prompt. A PASS here must not be read as row-34 prompt compliance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "infra/helm/llm-agent-platform/prompts"
RUNTIME_CONSUMPTION = {
    "status": "NOT_PROVEN",
    "reason": ("the BYO Coordinator sends the system prompt fixed in ai_platform/llm_agent/nl_intake.py and the "
               "SandboxAgent Specialists are typed-only; changing that is an application change outside CI/CD "
               "(frozen platform, owner decision D4)"),
}
Runner = Callable[[Sequence[str]], int]


def check_git_manifest(prompts: Path = PROMPTS) -> list[str]:
    manifest = json.loads((prompts / "manifest.json").read_text(encoding="utf-8"))
    problems = []
    entries = manifest.get("prompts", {})
    if not entries:
        problems.append("prompt manifest pins no prompts")
    for key, entry in sorted(entries.items()):
        path = prompts / entry.get("path", "")
        if not path.is_file():
            problems.append(f"{key}: {entry.get('path')} is missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
            problems.append(f"{key}: file bytes do not match the pinned sha256")
    return problems


def _run(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), cwd=ROOT, check=False).returncode


def check_registry(registry: str, run: Runner = _run) -> list[str]:
    if not registry:
        return ["no prompt registry URL (PROMPT_REGISTRY_URL); the registry was not verified"]
    code = run(["uv", "run", "--locked", "--project", "infra/docker/llm-agent-tooling", "python", "-m",
                "ops.llm_agent.prompt_registry", "verify-all", "--registry", registry])
    return [] if code == 0 else [f"prompt_registry verify-all exited {code}"]


def gate(registry: str, *, prompts: Path = PROMPTS, run: Runner = _run) -> dict:
    checks = {"git-manifest": check_git_manifest(prompts), "registry": check_registry(registry, run)}
    verdict = "PASS" if not any(checks.values()) else "FAIL"
    return {"unit": "prompt-registry-gate", "verdict": verdict, "environment": "rehearsal",
            "production_changed": False,
            "checks": {name: ("PASS" if not problems else "FAIL") for name, problems in checks.items()},
            "problems": [item for problems in checks.values() for item in problems],
            "runtime_consumption": RUNTIME_CONSUMPTION}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", default="")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--unit", default="prompt-registry-gate")
    args = parser.parse_args(argv)
    report = {**gate(args.registry), "release_sha": args.git_sha}
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    (args.evidence_dir / "verdict.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("unit", "verdict", "checks")}, sort_keys=True))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
