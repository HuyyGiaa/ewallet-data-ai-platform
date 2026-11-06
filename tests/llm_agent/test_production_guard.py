"""The O1, G2 and G1 scripts must refuse a cluster that runs the production release.

Each script runs for real against a fake ``helm`` and recording ``kubectl`` and ``gcloud``.
The important assertion is that nothing mutating was called, not only the exit code.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/llm_agent"
CONTEXT = "gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev"


def fake_bin(directory: Path, helm_body: str) -> Path:
    """Create fake tools that log each call."""

    bin_dir = directory / "bin"
    bin_dir.mkdir()
    log = directory / "calls.log"
    for name, body in {"helm": helm_body, "kubectl": "exit 0", "gcloud": "exit 0"}.items():
        path = bin_dir / name
        path.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{log}"\n{body}\n', encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def run(script: str, args: list[str], tmp_path: Path, helm_body: str, **env: str) -> tuple[int, str, list[str]]:
    """Run one script and return its exit code, stderr and recorded calls."""

    bin_dir = fake_bin(tmp_path, helm_body)
    result = subprocess.run(
        [str(SCRIPTS / script), *args], capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "KUBE_CONTEXT": CONTEXT, **env},
    )
    log = tmp_path / "calls.log"
    return result.returncode, result.stderr, log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def mutations(calls: list[str]) -> list[str]:
    """Calls other than the read-only ones the scripts make before the guard."""

    read_only = ("helm list", "kubectl config view", "gcloud container clusters describe")
    return [call for call in calls if not call.startswith(read_only)]


CASES = [
    ("o1_experiment.sh", {"DIGESTS": "/nonexistent", "O1_APPROVED": "yes"}, ["evidence"]),
    ("g1_operator.sh", {}, ["cleanup", "evidence"]),
    ("g1_operator.sh", {"G1_APPROVED_STAGES": "scale-up"}, ["scale-up", "evidence"]),
]


@pytest.mark.parametrize(("script", "env", "args"), CASES)
def test_refuses_when_production_release_exists(script, env, args, tmp_path) -> None:
    args = [str(tmp_path / item) if item == "evidence" else item for item in args]
    code, stderr, calls = run(script, args, tmp_path, 'echo llm-agent', **env)
    assert code != 0
    assert "production guard" in stderr
    assert mutations(calls) == []


@pytest.mark.parametrize(("script", "env", "args"), CASES)
def test_fails_closed_when_helm_cannot_answer(script, env, args, tmp_path) -> None:
    args = [str(tmp_path / item) if item == "evidence" else item for item in args]
    code, stderr, calls = run(script, args, tmp_path, 'exit 1', **env)
    assert code != 0
    assert "cannot list" in stderr
    assert mutations(calls) == []


def test_similar_release_name_is_not_production(tmp_path) -> None:
    guard = SCRIPTS / "production_guard.sh"
    bin_dir = fake_bin(tmp_path, 'echo llm-agent-mcp')
    result = subprocess.run(["bash", "-c", f'. "{guard}"; refuse_production_release ctx'], capture_output=True,
                            text=True, env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}, check=False)
    assert result.returncode == 0


def test_read_only_stage_is_not_guarded(tmp_path) -> None:
    _, stderr, _ = run("g1_operator.sh", ["evidence", str(tmp_path / "evidence")], tmp_path, 'echo llm-agent')
    assert "production guard" not in stderr
