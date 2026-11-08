"""Campaign safety paths of vllm_bench.sh against the fake cluster.

Covers what the 2026-11-09 evidence replay added: the in-cluster watchdog and its RBAC
check, the scale-to-zero pool, the laptop timer, the cost ceiling, owner checkpoints,
the smoke profile, observability and policy evidence, leaked port-forwards, leftover VMs
and the watchdog drill.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.experiments.test_vllm_bench_session import CONTEXT, DRIVER, IMAGE, SIM, calls

KEY_LIKE = "EWALLET_GATEWAY_KEY="


def run(tmp_path: Path, experiment: str = "exp-v1", action: str = "session", *, stdin: str | None = None,
        hold_stdin: bool = False, **env: str):
    """Run the driver; ``stdin`` feeds checkpoint answers, ``hold_stdin`` keeps the pipe open and silent."""

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    evidence = tmp_path / "evidence"
    full = {**os.environ, "PATH": f"{SIM / 'bin'}:{os.environ['PATH']}", "SIM_STATE": str(state),
            "KUBE_CONTEXT": CONTEXT, "VLLM_IMAGE": IMAGE, "EXP_APPROVED": "yes", "EXP_PYTHON": sys.executable,
            "EXP_CLIENT_DIR": str(SIM / "client"), "EXP_FORWARD_WAIT": "0", "EXP_RUNS": "2", "EXP_SCRAPE_SETTLE": "0",
            "EXP_LOCK_FILE": str(tmp_path / "cleanup.lock"), "EXP_DRILL_POLL_SECONDS": "0", **env}
    out_path, err_path = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
    with out_path.open("w") as out, err_path.open("w") as err:
        proc = subprocess.Popen([str(DRIVER), experiment, action, str(evidence)], stdin=subprocess.PIPE,
                                stdout=out, stderr=err, text=True, env=full)
        assert proc.stdin is not None
        if not hold_stdin:
            proc.stdin.write(stdin or "")
            proc.stdin.close()
        proc.wait(timeout=150)
        if hold_stdin:
            proc.stdin.close()
    result = subprocess.CompletedProcess(proc.args, proc.returncode, out_path.read_text(), err_path.read_text())
    return result, evidence, state


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}").exists() and "Z" not in Path(f"/proc/{pid}/stat").read_text().split()[2]


def test_pool_scales_to_zero_and_is_capped_at_the_planned_size(tmp_path) -> None:
    result, _, state = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(item for item in calls(state).splitlines() if "node-pools create" in item)
    assert "--num-nodes 1 --enable-autoscaling --min-nodes 0 --max-nodes 1" in line


def test_v2_pool_is_capped_at_two_nodes(tmp_path) -> None:
    result, _, state = run(tmp_path, experiment="exp-v2", action="plan")
    assert result.returncode == 0, result.stderr
    plan = json.loads((tmp_path / "evidence/plan/cost-plan.json").read_text(encoding="utf-8"))
    assert plan["nodes"] == 2 and plan["fits"] and plan["worst_case_usd"] < 5


def test_watchdog_is_armed_before_any_workload_with_the_grace_deadline(tmp_path) -> None:
    result, evidence, state = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    applied = (state / "applied.log").read_text(encoding="utf-8").splitlines()
    assert applied[0].endswith("safety/watchdog.yaml")
    assert all("watchdog" in item or "stack.yaml" in item or "config/run-" in item for item in applied)
    docs = list(yaml.safe_load_all((evidence / "safety/watchdog.yaml").read_text(encoding="utf-8")))
    target = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "bench-watchdog-target")
    assert target["data"]["MODE"] == "enforce"
    log = (evidence / "session.log").read_text(encoding="utf-8")
    limit = log.split("time limit ")[1].split(";")[0]
    from datetime import datetime, timedelta
    expected = datetime.fromisoformat(limit.replace("Z", "+00:00")) + timedelta(minutes=10)
    assert target["data"]["DEADLINE_UTC"] == expected.strftime("%Y-%m-%dT%H:%M:%SZ")
    rbac = (evidence / "safety/watchdog-rbac.txt").read_text(encoding="utf-8")
    assert "delete deployments -n ewallet-bench: yes" in rbac
    assert "delete deployments -n ewallet-agents: no" in rbac and "delete nodes: no" in rbac


def test_watchdog_with_broad_access_stops_before_any_workload(tmp_path) -> None:
    result, _, state = run(tmp_path, SIM_WATCHDOG_RBAC="broad")
    assert result.returncode != 0 and "more access than its own namespace" in result.stderr
    assert not (state / "clients.log").exists() and not (state / "bench-prometheus").exists()
    assert not (state / "pool").exists() and not (state / "namespace").exists()


def test_laptop_timer_is_armed_after_the_limit_and_disarmed_at_the_end(tmp_path) -> None:
    result, _, state = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    arm = next(item for item in calls(state).splitlines() if item.startswith("systemd-run"))
    assert "--user" in arm and "--on-calendar" in arm and arm.rstrip().endswith("safety/laptop-timer")
    assert " exp-v1 down " in arm
    assert any(item.startswith("systemctl --user stop ewallet-bench-down-") for item in calls(state).splitlines())
    assert not (state / "laptop-timer").exists()


def test_laptop_timer_can_be_turned_off(tmp_path) -> None:
    result, _, state = run(tmp_path, EXP_LAPTOP_TIMER="off")
    assert result.returncode == 0, result.stderr
    assert "systemd-run" not in calls(state)


def test_secrets_are_created_without_a_trailing_newline(tmp_path) -> None:
    result, _, state = run(tmp_path)
    assert result.returncode == 0, result.stderr
    lines = (state / "secrets.log").read_text(encoding="utf-8").splitlines()
    assert {line.split()[0] for line in lines} == {"vllm-api-key", "agentgateway-client-key", "bench-grafana-admin"}
    assert all(line.endswith("newline=False") for line in lines)


@pytest.mark.parametrize(("env", "message"), [
    ({"EXP_MAX_MINUTES": "216"}, "EXP_MAX_MINUTES must be 1-215"),
    ({"EXP_MAX_USD": "6"}, "EXP_MAX_USD"),
    ({"EXP_MAX_USD": "0.10"}, "does not fit the ceilings"),
    ({"EXP_CAPTURE_MODE": "screenshots"}, "EXP_CAPTURE_MODE"),
    ({"EXP_CHECKPOINT_MINUTES": "90"}, "EXP_CHECKPOINT_MINUTES"),
    ({"EXP_PROFILE": "smoke", "EXP_RUNS": "2"}, "exactly one run"),
])
def test_ceilings_and_bad_settings_refuse_before_any_resource(tmp_path, env, message) -> None:
    result, _, state = run(tmp_path, **env)
    assert result.returncode != 0 and message in result.stderr
    assert not (state / "pool").exists()


def test_interactive_session_waits_at_every_checkpoint(tmp_path) -> None:
    result, evidence, state = run(tmp_path, EXP_CAPTURE_MODE="interactive", stdin="CONTINUE\n" * 5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr.count("OWNER EVIDENCE CHECKPOINT") == 4
    assert "COMPARISON CHECKPOINT" in result.stderr
    banner = (evidence / "baseline/logs/run-01/checkpoint.txt").read_text(encoding="utf-8")
    assert "Run ID:         baseline-01" in banner and "var-run_id=baseline-01" in banner and "&from=" in banner
    assert "screenshots -> " + str(evidence / "baseline/screenshots") in banner
    assert KEY_LIKE not in result.stderr and "c2ltdWxhdGVk" not in result.stderr
    assert not (state / "pool").exists()


def test_owner_abort_stops_and_cleans_up(tmp_path) -> None:
    result, evidence, state = run(tmp_path, EXP_CAPTURE_MODE="interactive", stdin="nope\nABORT\n")
    assert result.returncode != 0 and "aborted by the owner" in result.stderr
    assert len((state / "clients.log").read_text(encoding="utf-8").splitlines()) == 1
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert any("owner aborted" in item for item in manifest["deviations"])
    assert not (state / "pool").exists() and not (state / "namespace").exists()


def test_no_answer_times_out_and_cleans_up(tmp_path) -> None:
    result, evidence, state = run(tmp_path, EXP_CAPTURE_MODE="interactive", EXP_CHECKPOINT_TEST_SECONDS="2",
                                  hold_stdin=True)
    assert result.returncode != 0 and "checkpoint timed out" in result.stderr
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert any("no owner answer" in item for item in manifest["deviations"])
    assert not (state / "pool").exists()


def test_closed_input_in_interactive_mode_stops_instead_of_running_on(tmp_path) -> None:
    result, _, state = run(tmp_path, EXP_CAPTURE_MODE="interactive", stdin="")
    assert result.returncode != 0
    assert len((state / "clients.log").read_text(encoding="utf-8").splitlines()) == 1


def test_unattended_mode_never_reads_input(tmp_path) -> None:
    result, _, _ = run(tmp_path, hold_stdin=True)
    assert result.returncode == 0, result.stderr
    assert "OWNER EVIDENCE CHECKPOINT" not in result.stderr


def test_smoke_profile_is_never_a_result(tmp_path) -> None:
    result, evidence, state = run(tmp_path, EXP_PROFILE="smoke", EXP_RUNS="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len((state / "clients.log").read_text(encoding="utf-8").splitlines()) == 2
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert any(item.startswith("SMOKE profile") for item in manifest["deviations"])
    assert manifest["settings"]["levels"] == "1"
    assert json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))["verdict"] == "INCONCLUSIVE"


def test_observability_and_policy_evidence_are_written_and_no_forward_leaks(tmp_path) -> None:
    result, evidence, state = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (state / "bench-prometheus").exists()
    assert json.loads((evidence / "observability/dashboard/llm-cpu-benchmark.json").read_text(encoding="utf-8"))["uid"]
    for arm in ("baseline", "optimized"):
        readback = json.loads((evidence / f"{arm}/metrics/run-01/prometheus-readback.json").read_text(encoding="utf-8"))
        assert readback["run_id"] == f"{arm}-01" and readback["panels"]
        assert (evidence / f"gateway-policy/{arm}/results.json").exists()
    assert not (evidence / "gateway-policy/baseline/run-02").exists()
    pids = [int(item) for item in (state / "pf.pids").read_text(encoding="utf-8").split()]
    assert len(pids) >= 6 and not [pid for pid in pids if alive(pid)]
    text = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in evidence.rglob("*") if p.is_file())
    assert KEY_LIKE not in text and "c2ltdWxhdGVk" not in text


def test_leftover_bench_vm_fails_cleanup(tmp_path) -> None:
    result, _, _ = run(tmp_path, SIM_VM_LEFT="yes")
    assert result.returncode != 0
    assert "CLEANUP FAILED" in result.stdout


def test_down_reports_leftover_vm(tmp_path) -> None:
    result, _, _ = run(tmp_path, action="down", SIM_VM_LEFT="yes")
    assert result.returncode != 0 and "bench VMs or disks remain" in result.stderr


def test_drill_passes_when_the_cluster_empties_itself(tmp_path) -> None:
    result, evidence, state = run(tmp_path, action="drill")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (evidence / "safety/drill-result.txt").read_text(encoding="utf-8").strip() == "PASS"
    assert (state / "drill-idle").exists() and (state / "bench-watchdog").exists()
    assert not (state / "pool").exists() and not (state / "namespace").exists()
    assert "systemd-run" not in calls(state) and not (state / "clients.log").exists()
    timeline = (evidence / "safety/drill-timeline.tsv").read_text(encoding="utf-8").splitlines()
    assert timeline[-1].endswith("\t0\t0\t0")


def test_drill_that_never_reaches_zero_fails_and_still_cleans_up(tmp_path) -> None:
    result, evidence, state = run(tmp_path, action="drill", SIM_DRILL_STUCK="yes", EXP_DRILL_MAX_MINUTES="1",
                                  EXP_DRILL_POLL_SECONDS="5")
    assert result.returncode != 0 and "did not reach zero nodes" in result.stderr
    assert (evidence / "safety/drill-result.txt").read_text(encoding="utf-8").strip() == "FAIL"
    assert not (state / "pool").exists() and not (state / "namespace").exists()


def test_dns_egress_reaches_nodelocal_dns_and_the_kube_dns_service(tmp_path) -> None:
    result, evidence, _ = run(tmp_path)
    assert result.returncode == 0, result.stderr
    docs = list(yaml.safe_load_all((evidence / "baseline/config/run-01.yaml").read_text(encoding="utf-8")))
    dns = next(d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "allow-dns")
    peers = dns["spec"]["egress"][0]["to"]
    assert {"ipBlock": {"cidr": "169.254.20.10/32"}} in peers and {"ipBlock": {"cidr": "10.0.0.10/32"}} in peers
    assert peers[0]["podSelector"] == {"matchLabels": {"k8s-app": "kube-dns"}}


def test_failed_run_still_records_a_successful_cleanup(tmp_path) -> None:
    result, evidence, _ = run(tmp_path, SIM_FAIL_RUN="1")
    assert result.returncode != 0
    assert (evidence / "safety/cleanup.txt").read_text(encoding="utf-8").startswith("pool and namespace confirmed absent")
