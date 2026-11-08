"""Whole benchmark sessions against a fake cluster.

These run the real driver, renderer, preflight, readback and comparison. Only kubectl,
gcloud, helm and the two benchmark clients are replaced, so every stage and transition
is exercised before a billable session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "scripts/experiments/vllm_bench.sh"
SIM = ROOT / "tests/experiments/sim"
CONTEXT = "gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev"
IMAGE = ("asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/vllm-cpu-model@sha256:"
         + "a" * 64)
CHART = os.environ.get("G2_ROUTER_CHART")


def session(tmp_path: Path, experiment: str = "exp-v1", action: str = "session", **env: str):
    """Run the driver and return the process, the evidence directory and the state directory."""

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    evidence = tmp_path / "evidence"
    full = {**os.environ, "PATH": f"{SIM / 'bin'}:{os.environ['PATH']}", "SIM_STATE": str(state),
            "KUBE_CONTEXT": CONTEXT, "VLLM_IMAGE": IMAGE, "EXP_APPROVED": "yes", "EXP_PYTHON": sys.executable,
            "EXP_CLIENT_DIR": str(SIM / "client"), "EXP_FORWARD_WAIT": "0", "EXP_RUNS": "2", "EXP_SCRAPE_SETTLE": "0",
            "EXP_LOCK_FILE": str(tmp_path / "cleanup.lock"), "EXP_DRILL_POLL_SECONDS": "0", **env}
    result = subprocess.run([str(DRIVER), experiment, action, str(evidence)], capture_output=True, text=True,
                            env=full, timeout=120, check=False)
    return result, evidence, state


def calls(state: Path) -> str:
    return (state / "calls.log").read_text(encoding="utf-8")


def test_full_session_compares_and_cleans_up(tmp_path) -> None:
    result, evidence, state = session(tmp_path, EXP_RUNS="3")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))
    assert report["runs"] == {"baseline": 3, "optimized": 3}
    assert report["verdict"] == "IMPROVED"
    assert not (state / "pool").exists() and not (state / "namespace").exists()
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["finished_at"] and manifest["deviations"] == []


def test_two_runs_below_the_spec_minimum_stay_inconclusive(tmp_path) -> None:
    _, evidence, _ = session(tmp_path)
    assert json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))["verdict"] == "INCONCLUSIVE"


def test_arms_alternate(tmp_path) -> None:
    _, _, state = session(tmp_path)
    order = [line.split()[0] for line in (state / "clients.log").read_text(encoding="utf-8").splitlines()]
    assert order == ["b0-prime", "c1-thread-bind", "c1-thread-bind", "b0-prime"]


def test_every_run_restarts_vllm_with_its_own_run_id(tmp_path) -> None:
    _, evidence, _ = session(tmp_path)
    ids = {line for path in evidence.glob("*/config/run-*.yaml")
           for line in path.read_text(encoding="utf-8").splitlines() if "ewallet.io/run-id" in line}
    assert len(ids) == 4


def test_only_bench_objects_are_changed(tmp_path) -> None:
    _, _, state = session(tmp_path)
    for line in calls(state).splitlines():
        reads = (" list ", " describe ")
        if line.startswith("gcloud") and not any(word in line for word in reads):
            assert "node-pools create bench-pool" in line or "node-pools delete bench-pool" in line, line
        if " delete " in line or " uninstall " in line:
            assert "ewallet-bench" in line or "bench-pool" in line, line
    assert not any("llm-agent" in line for line in calls(state).splitlines() if not line.startswith("helm list"))


def test_secrets_never_reach_evidence_or_arguments(tmp_path) -> None:
    _, evidence, state = session(tmp_path)
    assert "--from-literal" not in calls(state)
    text = "".join(path.read_text(encoding="utf-8", errors="ignore") for path in evidence.rglob("*") if path.is_file())
    assert "EWALLET_GATEWAY_KEY=" not in text


def test_failed_run_stops_without_retry_and_still_cleans_up(tmp_path) -> None:
    result, evidence, state = session(tmp_path, SIM_FAIL_RUN="2")
    assert result.returncode != 0
    assert len((state / "clients.log").read_text(encoding="utf-8").splitlines()) == 2
    assert not (state / "pool").exists() and not (state / "namespace").exists()
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert any("client exit 7" in item for item in manifest["deviations"])
    report = json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "NOT_EXECUTED"


def test_time_limit_stops_the_campaign(tmp_path) -> None:
    result, evidence, state = session(tmp_path, SIM_STOP_AT="1")
    assert result.returncode != 0
    assert len((state / "clients.log").read_text(encoding="utf-8").splitlines()) == 1
    assert not (state / "pool").exists()
    assert "time limit" in (evidence / "manifest.json").read_text(encoding="utf-8")


def test_image_drift_stops_the_session_before_the_next_run(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    # A later run on another digest must stop before it sends a request.
    wrapper = tmp_path / "bin"
    wrapper.mkdir()
    (wrapper / "kubectl").write_text(
        f'#!/usr/bin/env bash\ncount=$(grep -c "get pods -l app.kubernetes.io/name=vllm --field" "{state}/calls.log" || true)\n'
        f'[ "$count" -ge 2 ] && export SIM_RUNNING_DIGEST=sha256:{"b" * 64}\nexec "{SIM}/bin/kubectl" "$@"\n', encoding="utf-8")
    (wrapper / "kubectl").chmod(0o755)
    (state / "calls.log").write_text("", encoding="utf-8")
    result, evidence, state = session(tmp_path, PATH=f"{wrapper}:{SIM / 'bin'}:{os.environ['PATH']}")
    assert result.returncode != 0 and "not running the pinned image" in result.stderr
    assert len((state / "clients.log").read_text(encoding="utf-8").splitlines()) == 2
    assert json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))["verdict"] == "INCONCLUSIVE"


def test_production_change_fails_the_session(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    # The client bumps the production revision, as an unrelated release during a session would.
    client = tmp_path / "client"
    client.mkdir()
    (client / "o1_benchmark.py").write_text(
        f'import pathlib, runpy\npathlib.Path("{state}/prod-revision").write_text("42")\n'
        f'runpy.run_path("{SIM}/client/o1_benchmark.py", run_name="__main__")\n', encoding="utf-8")
    result, _, _ = session(tmp_path, EXP_CLIENT_DIR=str(client))
    assert result.returncode != 0
    assert "PRODUCTION STATE CHANGED" in result.stdout


def test_pool_matches_the_live_node_identity(tmp_path) -> None:
    _, _, state = session(tmp_path)
    line = next(item for item in calls(state).splitlines() if "node-pools create" in item)
    for flag in ("--service-account ewallet-gke-nodes@", "--workload-metadata GKE_METADATA", "--shielded-secure-boot",
                 "--node-labels ewallet.io/bench=true", "--node-taints ewallet.io/bench=true:NoSchedule"):
        assert flag in line
    assert "--no-enable-autoupgrade" not in line


def test_gcloud_failure_during_cleanup_is_not_a_false_success(tmp_path) -> None:
    result, _, _ = session(tmp_path, SIM_GCLOUD_DOWN="yes")
    assert result.returncode != 0
    assert "CLEANUP FAILED" in result.stdout


def test_pool_without_our_mark_is_never_deleted(tmp_path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state/pool").write_text("up", encoding="utf-8")
    result, _, state = session(tmp_path, action="down", SIM_POOL_LABELS="workload=someone-else")
    assert result.returncode != 0 and "leaving it" in result.stderr
    assert (state / "pool").exists()
    assert "node-pools delete" not in calls(state)


def test_namespace_without_our_mark_is_never_deleted(tmp_path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state/namespace").write_text("true", encoding="utf-8")
    result, _, state = session(tmp_path, action="down", SIM_NAMESPACE_MARK="")
    assert result.returncode != 0 and "leaving it" in result.stderr
    assert "delete namespace" not in calls(state)


def test_down_removes_only_marked_objects(tmp_path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state/pool").write_text("up", encoding="utf-8")
    (tmp_path / "state/namespace").write_text("true", encoding="utf-8")
    result, _, state = session(tmp_path, action="down")
    assert result.returncode == 0, result.stderr
    assert not (state / "pool").exists() and not (state / "namespace").exists()


@pytest.mark.parametrize("env", [{"VLLM_IMAGE": "docker.io/other/vllm@sha256:" + "a" * 64}, {"SIM_IMAGE_MISSING": "yes"}],
                         ids=["foreign-repository", "not-in-registry"])
def test_unproven_image_stops_before_any_resource(tmp_path, env) -> None:
    result, _, state = session(tmp_path, **env)
    assert result.returncode != 0
    assert not (state / "pool").exists()


@pytest.mark.parametrize("env", [{"SIM_RUNNING_DIGEST": "sha256:" + "b" * 64}, {"SIM_MODEL_REVISION": "other"}],
                         ids=["other-image", "other-model"])
def test_wrong_running_image_or_model_stops_before_any_request(tmp_path, env) -> None:
    result, _, state = session(tmp_path, **env)
    assert result.returncode != 0
    assert not (state / "clients.log").exists()
    assert not (state / "pool").exists()


def test_replaced_node_makes_the_comparison_invalid(tmp_path) -> None:
    result, evidence, _ = session(tmp_path, SIM_NODE_REPLACED="yes", EXP_RUNS="3")
    assert result.returncode == 0, result.stderr
    report = json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "INVALID_COMPARISON"
    assert any("node_names" in item for item in report["reasons"])


def test_summary_that_drops_raw_rows_blocks_the_verdict(tmp_path) -> None:
    result, evidence, _ = session(tmp_path, SIM_DROP_RAW="1", EXP_RUNS="3")
    assert result.returncode == 0, result.stderr
    report = json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "INCONCLUSIVE"
    assert any("RAW_ROW_MISMATCH" in item for item in report["reasons"])


def test_port_in_use_stops_the_session(tmp_path) -> None:
    import socket

    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 13300))
        listener.listen()
        result, _, state = session(tmp_path)
    assert result.returncode != 0 and "13300" in result.stderr
    assert not (state / "pool").exists()


@pytest.mark.parametrize("path", sorted((ROOT / "scripts/experiments").glob("*.sh")), ids=lambda p: p.name)
def test_no_driver_can_touch_production_objects(path) -> None:
    """Static check on every mutating line, so a new command cannot slip past the simulation."""

    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text.startswith("#"):
            continue
        assert "resize" not in text, text
        assert "llm-agent" not in text or "--filter" in text or "feature" in text, text
        if "helm uninstall" in text or "helm upgrade" in text:
            assert "llmd-router" in text, text
        if "node-pools delete" in text or "node-pools create" in text or "node-pools update" in text:
            assert '"$POOL"' in text, text


def test_real_client_keeps_failed_requests_in_raw_and_summary(tmp_path, monkeypatch) -> None:
    sys.path.insert(0, str(ROOT / "scripts/llm_agent"))
    import o1_benchmark as o1
    from ops.experiments.adapters import check_raw_rows, run_from_summary

    def fake(endpoint, key, item, *, timeout, level):
        failed = item["id"].endswith("0")
        return {"id": item["id"], "category": item["category"], "level": level, "backend": "primary", "failed": failed,
                "timeout": False, "error": "boom" if failed else None, "status": 500 if failed else 200, "e2e_ms": 10.0,
                "ttft_ms": 1.0, "itl_ms": [1.0], "tpot_ms": 1.0, "input_tokens": 3, "output_tokens": 2}

    monkeypatch.setattr(o1, "one_request", fake)
    items = [{"id": f"item-{index}", "category": "tool_call"} for index in range(12)]
    raw = tmp_path / "raw_requests.jsonl"
    rows = o1.run_pass("http://x", "k", items, 2, 5, tmp_path / "stop", raw)
    level = {**o1.summarize_level(rows, [], 1.0, {}, 2.0), "status": "MEASURED"}
    summary = {"config": "c", "workload_sha256": "w", "levels": {"2": level}}
    run = check_raw_rows(run_from_summary(summary, {}), summary, raw)
    assert run.levels["2"]["status"] == "MEASURED"
    assert run.levels["2"]["requests"] == 12 and run.levels["2"]["failed"] == 2


def test_failed_pool_deletion_is_reported(tmp_path) -> None:
    result, _, _ = session(tmp_path, SIM_DELETE_FAILS="yes")
    assert result.returncode != 0
    assert "CLEANUP FAILED" in result.stdout


@pytest.mark.parametrize(("env", "message"), [
    ({"EXP_APPROVED": ""}, "EXP_APPROVED"),
    ({"KUBE_CONTEXT": "other"}, "KUBE_CONTEXT"),
    ({"EXP_RUNS": "1"}, "one run cannot"),
    ({"EXP_MAX_MINUTES": "900"}, "EXP_MAX_MINUTES"),
])
def test_refusals_touch_nothing(tmp_path, env, message) -> None:
    result, _, state = session(tmp_path, **env)
    assert result.returncode != 0 and message in result.stderr
    assert not (state / "pool").exists() and not (state / "namespace").exists()


def test_existing_evidence_is_not_overwritten(tmp_path) -> None:
    (tmp_path / "evidence").mkdir()
    result, _, state = session(tmp_path)
    assert result.returncode != 0 and "never overwritten" in result.stderr
    assert not (state / "pool").exists()


def test_plan_touches_no_cluster_and_shows_the_single_difference(tmp_path) -> None:
    result, evidence, state = session(tmp_path, action="plan", KUBE_CONTEXT="", EXP_APPROVED="")
    assert result.returncode == 0, result.stderr
    assert not (state / "calls.log").exists()
    diff = (evidence / "plan/arm-difference.diff").read_text(encoding="utf-8")
    changed = [line for line in diff.splitlines() if line[:1] in "<>"]
    assert len(changed) == 2 and all("THREADS_BIND" in line for line in changed)


def test_v2_plan_differs_only_in_the_route(tmp_path) -> None:
    result, evidence, _ = session(tmp_path, experiment="exp-v2", action="plan")
    assert result.returncode == 0, result.stderr
    changed = [line for line in (evidence / "plan/arm-difference.diff").read_text(encoding="utf-8").splitlines()
               if line[:1] in "<>"]
    assert len(changed) == 4
    assert all("baseUrl" in line or "x-ewallet-backend" in line for line in changed)


def test_rendered_stack_is_isolated(tmp_path) -> None:
    _, evidence, _ = session(tmp_path, action="plan")
    documents = list(yaml.safe_load_all((evidence / "plan/baseline.yaml").read_text(encoding="utf-8")))
    assert documents[0]["kind"] == "Namespace"
    for document in documents[1:]:
        assert document["metadata"]["namespace"] == "ewallet-bench"
    for document in (item for item in documents if item["kind"] == "Deployment"):
        pod = document["spec"]["template"]["spec"]
        assert pod["nodeSelector"] == {"ewallet.io/bench": "true"}
        assert pod["tolerations"][0]["key"] == "ewallet.io/bench"
    assert {"ClusterRole", "ClusterRoleBinding"}.isdisjoint(item["kind"] for item in documents)


@pytest.mark.skipif(not CHART, reason="G2_ROUTER_CHART unavailable")
def test_v2_session_installs_router_only_for_optimized(tmp_path) -> None:
    result, evidence, state = session(tmp_path, experiment="exp-v2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (evidence / "comparison/metrics.json").exists()
    log = calls(state)
    assert log.count("helm upgrade --install llmd-router") == 2
    assert "--epp-metrics" not in (evidence / "baseline/logs/run-01/client.log").read_text(encoding="utf-8")


@pytest.mark.skipif(not CHART, reason="G2_ROUTER_CHART unavailable")
def test_router_values_place_it_on_the_bench_pool() -> None:
    rendered = subprocess.run(
        ["helm", "template", "llmd-router", CHART, "-n", "ewallet-bench",
         "-f", str(ROOT / "infra/helm/llm-d/standalone-values-g2.yaml"),
         "-f", str(ROOT / "infra/kubernetes/experiments/llmd-bench-values.yaml")],
        capture_output=True, text=True, check=True, timeout=60).stdout
    documents = [item for item in yaml.safe_load_all(rendered) if item]
    deployment = next(item for item in documents if item["kind"] == "Deployment")
    assert deployment["spec"]["template"]["spec"]["tolerations"][0]["key"] == "ewallet.io/bench"
    assert "ewallet.io/bench" in str(deployment["spec"]["template"]["spec"]["affinity"])
    assert {item["kind"] for item in documents}.isdisjoint({"ClusterRole", "ClusterRoleBinding"})
