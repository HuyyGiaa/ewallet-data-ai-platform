"""The owner's tmux workflow: the real runner behaves the same inside tmux, and the
single-run launcher cannot start anything but a single-run session.

Both tests drive a private tmux server (``tmux -L``) and type CONTINUE with send-keys, the
way the owner types it. They are skipped when tmux is not installed.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from tests.experiments.test_vllm_bench_session import CONTEXT, DRIVER, IMAGE, SIM

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


class Tmux:
    """A private tmux server with one detached session."""

    def __init__(self) -> None:
        self.socket = f"ewb-test-{uuid.uuid4().hex[:8]}"

    def tmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", self.socket, *args], capture_output=True, text=True, check=False)

    def start(self, command: str, env: dict[str, str]) -> None:
        exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        assert self.tmux("new-session", "-d", "-s", "bench", "-x", "200", "-y", "50",
                         f"env {exports} bash -c {shlex.quote(command)}").returncode == 0

    def type(self, text: str) -> None:
        self.tmux("send-keys", "-t", "bench", text, "Enter")

    def stop(self) -> None:
        self.tmux("kill-server")


def wait_for(predicate, timeout: float = 120) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.3)
    return False


def sim_env(tmp_path: Path) -> dict[str, str]:
    state = tmp_path / "state"
    state.mkdir()
    return {"PATH": f"{SIM / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"),
            "SIM_STATE": str(state), "KUBE_CONTEXT": CONTEXT, "VLLM_IMAGE": IMAGE, "EXP_APPROVED": "yes",
            "EXP_PYTHON": sys.executable, "EXP_CLIENT_DIR": str(SIM / "client"), "EXP_FORWARD_WAIT": "0",
            "EXP_SCRAPE_SETTLE": "0", "EXP_LOCK_FILE": str(tmp_path / "cleanup.lock"),
            "PYTHONPATH": str(ROOT)}


def answer_checkpoints(tmux: Tmux, log: Path, count: int) -> None:
    for number in range(1, count + 1):
        assert wait_for(lambda: log.is_file() and log.read_text().count("Type CONTINUE") >= number), \
            f"checkpoint {number} never appeared"
        tmux.type("CONTINUE")


def test_real_runner_inside_tmux_takes_continue_and_finishes(tmp_path) -> None:
    env, evidence, out = sim_env(tmp_path), tmp_path / "evidence", tmp_path / "pane.log"
    env.update(EXP_PROFILE="single", EXP_RUNS="1", EXP_CAPTURE_MODE="interactive", EXP_CHECKPOINT_RUNS="first")
    tmux = Tmux()
    try:
        tmux.start(f"{DRIVER} exp-v1 session {evidence} 2>&1 | tee {out}; echo done > {tmp_path}/done", env)
        answer_checkpoints(tmux, out, 3)
        assert wait_for(lambda: (tmp_path / "done").exists())
    finally:
        tmux.stop()
    assert "session end status=0" in out.read_text()
    report = json.loads((evidence / "comparison/metrics.json").read_text())
    assert report["methodology"] == "single-run" and report["verdict"] == "OBSERVED_IMPROVEMENT"
    assert (evidence / "session.log").read_text().count("owner typed CONTINUE") == 3
    assert not (Path(env["SIM_STATE"]) / "pool").exists()


def launcher_repo(tmp_path: Path) -> Path:
    """A git repo with the real launcher and a fake runner that records what it was given."""

    repo = tmp_path / "repo"
    (repo / "scripts/experiments").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/experiments/owner_single_run.sh", repo / "scripts/experiments/owner_single_run.sh")
    runner = repo / "scripts/experiments/vllm_bench.sh"
    runner.write_text("""#!/bin/bash
env | grep -E '^EXP_(PROFILE|RUNS|CAPTURE_MODE|CHECKPOINT_RUNS|MAX_MINUTES)=' | sort > "$3.env"
echo "args $*"
echo "Type CONTINUE to proceed"
read -r answer
echo "answer=$answer" >> "$3.env"
""")
    runner.chmod(0o755)
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["add", "."], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def test_launcher_forces_single_run_whatever_the_shell_says(tmp_path) -> None:
    repo = launcher_repo(tmp_path)
    tmux = Tmux()
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), "KUBE_CONTEXT": CONTEXT,
           "VLLM_IMAGE": IMAGE, "EXP_APPROVED": "yes", "OWNER_INHIBIT": "off",
           # A shell left over from the multi-run rehearsal must not leak into this session.
           "EXP_PROFILE": "formal", "EXP_RUNS": "3", "EXP_CAPTURE_MODE": "unattended"}
    log = repo / "camp/exp-v1.terminal.log"
    try:
        tmux.start(f"cd {repo} && scripts/experiments/owner_single_run.sh exp-v1 camp; echo $? > {tmp_path}/rc", env)
        answer_checkpoints(tmux, log, 1)
        assert wait_for(lambda: (tmp_path / "rc").exists())
    finally:
        tmux.stop()
    assert (tmp_path / "rc").read_text().strip() == "0"
    recorded = (repo / "camp/exp-v1.env").read_text().splitlines()
    assert recorded == ["EXP_CAPTURE_MODE=interactive", "EXP_CHECKPOINT_RUNS=first", "EXP_MAX_MINUTES=215",
                        "EXP_PROFILE=single", "EXP_RUNS=1", "answer=CONTINUE"]
    text = log.read_text()
    assert "args exp-v1 session camp/exp-v1" in text and "runner exit 0" in text
    assert (repo / "camp/screenshots").is_dir()


def run_launcher(repo: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    full = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), "KUBE_CONTEXT": CONTEXT,
            "VLLM_IMAGE": IMAGE, "EXP_APPROVED": "yes", "OWNER_INHIBIT": "off", "TMUX": "/tmp/x,1,0", **env}
    return subprocess.run([str(repo / "scripts/experiments/owner_single_run.sh"), *args], cwd=repo, env=full,
                          capture_output=True, text=True, timeout=30, check=False, stdin=subprocess.DEVNULL)


def test_launcher_refusals_start_nothing(tmp_path) -> None:
    repo = launcher_repo(tmp_path)
    assert "start tmux first" in run_launcher(repo, "exp-v1", "camp", TMUX="").stderr
    assert "EXP_APPROVED" in run_launcher(repo, "exp-v1", "camp", EXP_APPROVED="").stderr
    assert "usage" in run_launcher(repo, "exp-v3", "camp").stderr
    assert "inside" in run_launcher(repo, "exp-v1", "/elsewhere/camp").stderr
    (repo / "stray.txt").write_text("x")
    assert "source changes outside camp" in run_launcher(repo, "exp-v1", "camp").stderr
    (repo / "stray.txt").unlink()
    (repo / "camp/exp-v1").mkdir(parents=True)
    assert "never overwritten" in run_launcher(repo, "exp-v1", "camp").stderr
    assert not (repo / "camp/exp-v1.env").exists()


def test_evidence_inside_the_campaign_does_not_block_the_second_experiment(tmp_path) -> None:
    repo = launcher_repo(tmp_path)
    (repo / "camp/exp-v1").mkdir(parents=True)
    (repo / "camp/exp-v1/manifest.json").write_text("{}")
    result = run_launcher(repo, "exp-v2", "camp")
    # stdin is closed, so the fake runner's read ends at once; the launcher itself started it.
    assert "source changes outside" not in result.stderr
    assert "EXP_PROFILE=single" in (repo / "camp/exp-v2.env").read_text()


def interrupt_repo(tmp_path: Path) -> Path:
    """A fake runner that cleans up for a while after Ctrl-C, printing as it goes."""

    repo = launcher_repo(tmp_path)
    (repo / "scripts/experiments/vllm_bench.sh").write_text("""#!/bin/bash
cleanup() { for step in 1 2 3; do echo "cleanup step $step"; sleep 0.3; done; echo "session end status=130"; exit 130; }
trap cleanup INT
echo "Type CONTINUE to proceed"
while :; do sleep 0.2; done
""")
    subprocess.run(["git", "commit", "-qam", "interruptible"], cwd=repo, check=True, capture_output=True)
    return repo


def inhibit_works() -> bool:
    return bool(shutil.which("systemd-inhibit")) and subprocess.run(
        ["systemd-inhibit", "--list", "--no-pager"], capture_output=True, check=False).returncode == 0


def locks() -> str:
    return subprocess.run(["systemd-inhibit", "--list", "--no-pager"], capture_output=True, text=True,
                          check=False).stdout


def test_ctrl_c_lets_the_runner_clean_up_and_keeps_the_log(tmp_path) -> None:
    repo = interrupt_repo(tmp_path)
    tmux = Tmux()
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), "KUBE_CONTEXT": CONTEXT,
           "VLLM_IMAGE": IMAGE, "EXP_APPROVED": "yes", "OWNER_INHIBIT": "on" if inhibit_works() else "off"}
    log = repo / "camp/exp-v1.terminal.log"
    try:
        tmux.start(f"cd {repo} && scripts/experiments/owner_single_run.sh exp-v1 camp; echo $? > {tmp_path}/rc", env)
        assert wait_for(lambda: log.is_file() and "Type CONTINUE" in log.read_text())
        if env["OWNER_INHIBIT"] == "on":
            assert "single-run evidence session" in locks()
        tmux.tmux("send-keys", "-t", "bench", "C-c")
        assert wait_for(lambda: (tmp_path / "rc").exists(), 30)
    finally:
        tmux.stop()
    text = log.read_text()
    assert "cleanup step 3" in text and "session end status=130" in text and "runner exit 130" in text
    assert (tmp_path / "rc").read_text().strip() == "130"
    if env["OWNER_INHIBIT"] == "on":
        assert wait_for(lambda: "single-run evidence session" not in locks(), 10)
