"""Offline dry run of the EXP-V1 -> EXP-V2 chain: fakes for the cloud, the runner and systemd."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_chain_gate import build  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN = "docs/evidence/rehearsal"
HELM = '[{"name":"llm-agent","revision":"42"}]'


def setup(tmp: Path, v1_ok: bool = True) -> tuple[Path, Path, dict]:
    repo = tmp / "repo"
    (repo / "scripts/experiments").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/experiments/bench_chain.sh", repo / "scripts/experiments/bench_chain.sh")
    (repo / "ops").symlink_to(ROOT / "ops")
    runner = repo / "scripts/experiments/vllm_bench.sh"
    runner.write_text("""#!/bin/sh
echo "runner $*" >> "$HOME/calls.log"
case "$2" in plan) mkdir -p "$3/plan"; echo '{"fits": true, "problems": [], "worst_case_usd": 1.85}' > "$3/plan/cost-plan.json";; esac
""")
    runner.chmod(0o755)
    home, bin_dir = tmp / "home", tmp / "bin"
    (home / ".local/share/ewallet-bench").mkdir(parents=True)
    bin_dir.mkdir()
    fakes = {
        "systemctl": "exit 3",  # unit inactive; no timers
        "gcloud": """case "$*" in *node-pools*) echo cpu-pool;; *project-info*) echo '{"quotas":[{"metric":"CPUS_ALL_REGIONS","usage":20,"limit":32}]}';; *) echo prod-vm;; esac""",
        "kubectl": 'echo \'Error from server (NotFound): namespaces "ewallet-bench" not found\'',
        "helm": f"echo '{HELM}'",
        "pgrep": "exit 1",
    }
    for name, body in fakes.items():
        (bin_dir / name).write_text(f"#!/bin/sh\necho \"{name} $*\" >> \"$HOME/calls.log\"\n{body}\n")
        (bin_dir / name).chmod(0o755)
    campaign = repo / CAMPAIGN
    v1 = build(campaign, verdict="REGRESSED" if v1_ok else "INCONCLUSIVE")
    (v1 / "production-before.txt").write_text(HELM + "\nnode/a\n")
    (v1 / "production-after.txt").write_text(HELM + "\nnode/a\n")
    (campaign / "exp-v1.terminal.log").write_text("x\nrunner exit 0 at 2026-11-09T12:10:00Z\n")
    # The EXP-V2 launcher: builds a complete EXP-V2 session in the campaign directory.
    launcher = home / ".local/share/ewallet-bench/run-formal.sh"
    launcher.write_text(f"""#!/bin/sh
echo "run-formal $*" >> "$HOME/calls.log"
cd {repo}
"{sys.executable}" -c "import sys; sys.path.insert(0, '{Path(__file__).parent}'); from test_chain_gate import build; from pathlib import Path; s = build(Path('{campaign}/v2tmp'), levels=('1','2','4','8')); s.rename('{campaign}/exp-v2'); Path('{campaign}/v2tmp').rmdir()"
printf '%s\\nnode/a\\n' '{HELM}' > {campaign}/exp-v2/production-before.txt
cp {campaign}/exp-v2/production-before.txt {campaign}/exp-v2/production-after.txt
echo "runner exit 0 at now" > {campaign}/exp-v2.terminal.log
""")
    launcher.chmod(0o755)
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"], ["add", "scripts", "ops"],
                 ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "VLLM_IMAGE": "img@sha256:x",
           "KUBECONFIG": "/dev/null"}
    return repo, home, env


def chain(repo: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([str(repo / "scripts/experiments/bench_chain.sh"), CAMPAIGN, "v1.service"],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=120, check=False)


def test_chain_starts_exp_v2_once_and_commits_both(tmp_path) -> None:
    repo, home, env = setup(tmp_path)
    result = chain(repo, env)
    log = (home / ".local/share/ewallet-bench/chain-rehearsal.log").read_text()
    assert result.returncode == 0, log
    assert "EXP-V2 passed the gate" in log
    calls = (home / "calls.log").read_text()
    assert calls.count("run-formal exp-v2") == 1
    commits = subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout
    assert "EXP-V1 formal rehearsal" in commits and "chain record" in commits
    assert json.loads((repo / CAMPAIGN / "chain-gate-exp-v2.json").read_text())["passed"]
    # A second start is refused by the marker and never reaches the runner.
    assert chain(repo, env).returncode == 1
    assert (home / "calls.log").read_text().count("run-formal exp-v2") == 1


def test_invalid_exp_v1_never_starts_exp_v2(tmp_path) -> None:
    repo, home, env = setup(tmp_path, v1_ok=False)
    assert chain(repo, env).returncode == 1
    log = (home / ".local/share/ewallet-bench/chain-rehearsal.log").read_text()
    assert "EXP-V1 did not pass the gate" in log
    assert "run-formal" not in (home / "calls.log").read_text()
    assert not (home / ".local/share/ewallet-bench/chain-rehearsal.exp-v2-started").exists()
