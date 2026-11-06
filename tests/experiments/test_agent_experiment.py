"""Agent load, scaling summary, Sandbox manifests and KEDA access rules."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError

import pytest
import yaml

from ops.experiments import agent_load as load
from ops.experiments import agent_summary as summary

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "infra/kubernetes/agent-sandbox"
DRIVER = ROOT / "scripts/experiments/agent_experiment.sh"
TEMPLATE = (ROOT / "configs/experiments/fixtures/agent-load-request.json").read_text(encoding="utf-8")
CONTEXT = "gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev"
IMAGE = "repo/agent@sha256:" + "c" * 64


def run_one(post) -> dict:
    return load.run_task(post, TEMPLATE, "t-1", timeout=1, poll_timeout=1, poll_interval=0, sleep=lambda _: None)


def test_fixture_is_a_valid_business_request_for_each_task() -> None:
    from ai_platform.llm_agent.business_request import business_request_from_a2a

    payload = load.task_payload(TEMPLATE, "task-7")
    assert "__TASK_ID__" not in json.dumps(payload)
    assert business_request_from_a2a(payload) is not None


def test_completed_task_is_a_success() -> None:
    row = run_one(lambda message, timeout: {"result": {"id": "r", "status": {"state": "completed"}}})
    assert row["ok"] is True


def test_working_task_is_polled_to_its_end() -> None:
    replies = iter([{"result": {"id": "r", "status": {"state": "working"}}},
                    {"result": {"id": "r", "status": {"state": "failed"}}}])
    row = run_one(lambda message, timeout: next(replies))
    assert row["state"] == "failed" and row["ok"] is False


def test_task_that_never_ends_is_not_a_success() -> None:
    clock = iter(range(100))
    row = load.run_task(lambda message, timeout: {"result": {"id": "r", "status": {"state": "working"}}}, TEMPLATE, "t",
                        timeout=1, poll_timeout=3, poll_interval=0, clock=lambda: next(clock), sleep=lambda _: None)
    assert row["ok"] is False


@pytest.mark.parametrize("reply", [{"error": {"code": -32000}}, {"result": "text"}])
def test_rpc_error_is_a_failure(reply) -> None:
    assert run_one(lambda message, timeout: reply)["ok"] is False


def test_transport_failure_is_recorded_not_raised() -> None:
    def fail(message, timeout):
        raise URLError("refused")

    row = run_one(fail)
    assert row["state"] == "transport_error" and row["error"] == "URLError"


def test_request_cap_bounds_the_load() -> None:
    rows: list[str] = []
    sent = load.run_load(lambda message, timeout: {"result": {"id": "r", "status": {"state": "completed"}}}, TEMPLATE,
                         run_id="r", concurrency=3, duration=30, max_requests=7, timeout=1, poll_timeout=1,
                         poll_interval=0, emit=rows.append)
    assert sent == 7 and len(rows) == 7
    assert len({json.loads(row.split(" ", 1)[1])["id"] for row in rows}) == 7


def row(start: float, end: float, ok: bool = True) -> dict:
    return {"started": start, "finished": end, "ok": ok, "state": "completed" if ok else "failed",
            "e2e_ms": (end - start) * 1000}


def test_failures_stay_in_the_counts_but_not_in_latency() -> None:
    level = summary.summarize([row(0, 1), row(0, 100, False)], [], workload_sha256="w")["levels"]["load"]
    assert level["requests"] == 2 and level["succeeded"] == 1
    assert level["availability_percent"] == 50
    assert level["e2e_ms"]["p95"] == 1000
    assert level["states"] == {"completed": 1, "failed": 1}


def test_no_requests_is_not_measured() -> None:
    assert summary.summarize([], [], workload_sha256="w")["levels"]["load"]["status"] == "NO_REQUESTS"


def test_scale_out_and_in_are_timed() -> None:
    samples = [{"ts": 0, "ready": 2}, {"ts": 40, "ready": 3}, {"ts": 110, "ready": 3}, {"ts": 200, "ready": 2}]
    assert summary.scaling(samples, 10, 100) == {"peak_replicas": 3, "scale_out_seconds": 30, "scale_in_seconds": 100}


def test_no_scale_out_has_no_time_rather_than_zero() -> None:
    assert summary.scaling([{"ts": 0, "ready": 2}, {"ts": 50, "ready": 2}], 10, 40) == {
        "peak_replicas": 2, "scale_out_seconds": None, "scale_in_seconds": None}


def test_recovery_is_measured_from_the_injected_failure() -> None:
    rows = [row(0, 5), row(6, 12, False), row(12, 20)]
    assert summary.recovery_seconds(rows, 10) == 2


def test_no_failures_recover_in_zero_and_no_success_never_recovers() -> None:
    assert summary.recovery_seconds([row(0, 20)], 10) == 0
    assert summary.recovery_seconds([row(0, 20, False)], 10) is None
    assert summary.recovery_seconds([row(0, 20)], None) is None


def rendered() -> list[dict]:
    output = subprocess.run([sys.executable, str(ROOT / "scripts/experiments/render_load.py"), "--image", IMAGE, "--run-id", "r"],
                            capture_output=True, text=True, check=True).stdout
    return json.loads(output)["items"]


def test_load_objects_share_the_cleanup_label_and_stay_in_namespace() -> None:
    items = rendered()
    assert {item["kind"] for item in items} >= {"Job", "AuthorizationPolicy", "NetworkPolicy", "ServiceAccount", "ConfigMap"}
    for item in items:
        assert item["metadata"]["labels"] == {"ewallet.io/experiment-load": "true"}
        assert item["metadata"]["namespace"] == "ewallet-agents"


def test_load_job_runs_once_and_is_restricted() -> None:
    job = next(item for item in rendered() if item["kind"] == "Job")
    assert job["spec"]["backoffLimit"] == 0
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    security = pod["containers"][0]["securityContext"]
    assert security["allowPrivilegeEscalation"] is False and security["capabilities"]["drop"] == ["ALL"]


def test_load_policy_admits_only_the_load_identity() -> None:
    policy = next(item for item in rendered() if item["kind"] == "AuthorizationPolicy")
    assert policy["spec"]["rules"][0]["from"][0]["source"]["principals"] == ["cluster.local/ns/ewallet-agents/sa/agent-load"]


def test_load_hash_changes_with_the_profile() -> None:
    def digest(*args: str) -> str:
        return subprocess.run([sys.executable, str(ROOT / "scripts/experiments/render_load.py"), "--print-hash", *args],
                              capture_output=True, text=True, check=True).stdout

    assert digest() != digest("--concurrency", "8")


def driver(*args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(DRIVER), *args], capture_output=True, text=True, timeout=30, check=False,
                          env={**os.environ, "EXP_PYTHON": sys.executable, **env})


def test_plan_needs_no_cluster(tmp_path) -> None:
    result = driver("exp-c5-keda", "baseline", "plan", str(tmp_path / "e"), KUBE_CONTEXT="")
    assert result.returncode == 0 and "load_profile_sha256=" in result.stdout
    assert not (tmp_path / "e").exists()


def test_run_needs_approval(tmp_path) -> None:
    result = driver("exp-c5-keda", "baseline", "run", str(tmp_path / "e"), KUBE_CONTEXT=CONTEXT, EXP_APPROVED="")
    assert result.returncode != 0 and "EXP_APPROVED" in result.stderr


def documents(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def test_keda_access_adds_exactly_one_flow_each() -> None:
    items = documents(ROOT / "infra/kubernetes/experiments/keda-prometheus-access.yaml")
    assert sorted(item["kind"] for item in items) == ["AuthorizationPolicy", "NetworkPolicy"]
    policy = next(item for item in items if item["kind"] == "AuthorizationPolicy")
    assert policy["spec"]["rules"][0]["from"][0]["source"]["principals"] == ["cluster.local/ns/keda/sa/keda-operator"]


def test_keda_operator_pod_alone_joins_the_mesh() -> None:
    values = yaml.safe_load((ROOT / "infra/helm/keda/values.yaml").read_text(encoding="utf-8"))
    assert values["podLabels"] == {"keda": {"istio.io/dataplane-mode": "ambient"}}


def simulate(tmp_path: Path, experiment: str, arm: str, **env: str):
    """Run the real driver against the fake cluster."""

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    evidence = tmp_path / "evidence"
    full = {"PATH": f"{ROOT / 'tests/experiments/sim/agent_bin'}:{os.environ['PATH']}", "SIM_STATE": str(state),
            "KUBE_CONTEXT": CONTEXT, "EXP_APPROVED": "yes", "EXP_SCALE_IN_WAIT": "0", **env}
    action = full.pop("ACTION", "run")
    return driver(experiment, arm, action, str(evidence), **full), evidence, state


def test_keda_arm_runs_and_removes_its_load_objects(tmp_path) -> None:
    result, evidence, state = simulate(tmp_path, "exp-c5-keda", "baseline")
    assert result.returncode == 0, result.stderr
    level = json.loads((evidence / "baseline/raw/run-01/summary.json").read_text(encoding="utf-8"))["levels"]["load"]
    assert level["requests"] == 4 and level["succeeded"] == 3
    controls = json.loads((evidence / "baseline/raw/run-01/controls.json").read_text(encoding="utf-8"))
    assert controls["min_replicas"] == "2" and controls["node_type"] == "e2-standard-4"
    assert not (state / "applied").exists()
    assert not (state / "deleted-pod").exists()
    assert json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))["experiment_id"] == "EXP-C5-KEDA"


def test_second_run_gets_a_new_number(tmp_path) -> None:
    simulate(tmp_path, "exp-c5-keda", "baseline")
    result, evidence, _ = simulate(tmp_path, "exp-c5-keda", "baseline")
    assert result.returncode == 0, result.stderr
    assert (evidence / "baseline/raw/run-02/summary.json").exists()


@pytest.mark.parametrize(("arm", "env", "message"), [
    ("baseline", {"SIM_SCALED_OBJECT": "yes"}, "baseline needs the HPA"),
    ("optimized", {}, "optimized needs a ScaledObject"),
])
def test_keda_run_refuses_the_wrong_cluster_state(tmp_path, arm, env, message) -> None:
    result, _, state = simulate(tmp_path, "exp-c5-keda", arm, **env)
    assert result.returncode != 0 and message in result.stderr
    assert not (state / "applied").exists()


def test_short_job_not_seen_ready_is_still_measured(tmp_path) -> None:
    result, evidence, _ = simulate(tmp_path, "exp-c5-keda", "baseline", SIM_NEVER_READY="yes")
    assert result.returncode == 0, result.stderr
    assert (evidence / "baseline/raw/run-01/summary.json").exists()


def test_failed_job_keeps_its_rows_and_cleans_up(tmp_path) -> None:
    result, evidence, state = simulate(tmp_path, "exp-c5-keda", "baseline", SIM_JOB_FAILS="yes")
    assert result.returncode == 0, result.stderr
    assert (evidence / "baseline/raw/run-01/summary.json").exists()
    assert "job did not complete" in (evidence / "baseline/logs/raw-01/apply.log").read_text(encoding="utf-8")
    assert "load Job did not complete" in (evidence / "manifest.json").read_text(encoding="utf-8")
    assert not (state / "applied").exists()


def test_real_http_path_against_a_local_server() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            state = "working" if body["method"] == "message/send" else "completed"
            data = json.dumps({"result": {"id": "remote", "status": {"state": state}}}).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        row = load.run_task(load.http_post(f"http://127.0.0.1:{server.server_port}/"), TEMPLATE, "t", timeout=5,
                            poll_timeout=5, poll_interval=0)
    finally:
        server.shutdown()
    assert row["ok"] is True


def test_compare_reads_agent_runs(tmp_path) -> None:
    from ops.experiments.adapters import load_arm
    from ops.experiments.compare import compare
    from ops.experiments.spec import load_spec

    spec = load_spec(ROOT / "configs/experiments/exp-c5-keda.json")
    for arm in ("baseline", "optimized"):
        for _ in range(2):
            simulate(tmp_path, "exp-c5-keda", arm, SIM_SCALED_OBJECT="yes" if arm == "optimized" else "")
    evidence = tmp_path / "evidence"
    report = compare(spec, load_arm(evidence / "baseline"), load_arm(evidence / "optimized"))
    assert report["runs"] == {"baseline": 2, "optimized": 2}
    assert report["verdict"] != "INVALID_COMPARISON", report["reasons"]


def test_smoke_sends_one_task_and_stays_out_of_the_comparison(tmp_path) -> None:
    from ops.experiments.adapters import load_arm

    result, evidence, state = simulate(tmp_path, "exp-c5-keda", "baseline", ACTION="smoke", SIM_ONE_TASK="ok")
    assert result.returncode == 0, result.stderr
    assert "1/1 tasks completed" in result.stdout
    assert (evidence / "baseline/smoke/run-01/summary.json").exists()
    assert load_arm(evidence / "baseline") == []
    assert not (state / "applied").exists()


def test_failed_smoke_fails_the_command(tmp_path) -> None:
    result, _, state = simulate(tmp_path, "exp-c5-keda", "baseline", ACTION="smoke", SIM_ONE_TASK="fail")
    assert result.returncode != 0
    assert "0/1 tasks completed" in result.stdout
    assert not (state / "applied").exists()


def test_keda_driver_cannot_delete_pods() -> None:
    text = DRIVER.read_text(encoding="utf-8")
    assert "delete pod" not in text and "EXP_DISRUPT" not in text
