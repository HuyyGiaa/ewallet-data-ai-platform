"""The isolated MCP experiment: real driver and renderer against a fake cluster, and the real
probe against a real local HTTP server."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

from ai_platform.llm_agent.mcp_failover import FailoverPolicy
from ops.experiments import mcp_probe

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "scripts/experiments/mcp_experiment.sh"
SIM = ROOT / "tests/experiments/sim/mcp_bin"
CONTEXT = "gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev"
MCP = "repo/mcp@sha256:" + "a" * 64
PROBE = "repo/agent@sha256:" + "b" * 64


def session(tmp_path: Path, arm: str = "optimized", action: str = "run", **env: str):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    evidence = tmp_path / "evidence"
    full = {**os.environ, "PATH": f"{SIM}:{os.environ['PATH']}", "SIM_STATE": str(state), "KUBE_CONTEXT": CONTEXT,
            "MCP_IMAGE": MCP, "PROBE_IMAGE": PROBE, "EXP_APPROVED": "yes", "EXP_PYTHON": sys.executable,
            "EXP_FAILURE_AFTER": "0", "EXP_DURATION": "5", **env}
    result = subprocess.run([str(DRIVER), arm, action, str(evidence)], capture_output=True, text=True, env=full,
                            timeout=60, check=False)
    return result, evidence, state


def calls(state: Path) -> list[str]:
    return (state / "calls.log").read_text(encoding="utf-8").splitlines()


def test_run_measures_and_removes_the_namespace(tmp_path) -> None:
    result, evidence, state = session(tmp_path)
    assert result.returncode == 0, result.stderr
    level = json.loads((evidence / "optimized/raw/run-01/summary.json").read_text(encoding="utf-8"))["levels"]["load"]
    assert level["availability_percent"] == 100
    assert not (state / "namespace").exists()


def test_every_deletion_stays_in_the_experiment_namespace(tmp_path) -> None:
    _, _, state = session(tmp_path)
    deletions = [line for line in calls(state) if " delete " in line]
    assert deletions
    assert all("ewallet-mcp-exp" in line for line in deletions)


def test_source_cannot_name_a_production_namespace_in_a_delete() -> None:
    lines = [line for line in DRIVER.read_text(encoding="utf-8").splitlines() if " delete " in line]
    assert lines and not any("PROD_NS" in line or "ewallet-mcp " in line for line in lines)


def test_token_is_not_on_any_command_line_or_in_evidence(tmp_path) -> None:
    _, evidence, state = session(tmp_path)
    assert "--from-literal" not in "\n".join(calls(state))
    text = "".join(path.read_text(encoding="utf-8", errors="ignore") for path in evidence.rglob("*") if path.is_file())
    assert '"token":"' not in text


def test_arms_differ_only_in_replicas_and_attempts(tmp_path) -> None:
    def rendered(arm: str) -> list[str]:
        return subprocess.run([sys.executable, str(ROOT / "scripts/experiments/render_mcp.py"), arm, "--mcp-image", MCP,
                               "--probe-image", PROBE, "--release-sha", "s"], capture_output=True, text=True,
                              check=True).stdout.splitlines()

    base, candidate = rendered("baseline"), rendered("optimized")
    changed = [pair for pair in zip(base, candidate) if pair[0] != pair[1]]
    assert len(base) == len(candidate) and len(changed) == 2
    assert "replicas" in changed[0][0] and "EWALLET_MCP_MAX_ATTEMPTS" in changed[1][0]


def test_rendered_objects_stay_in_the_namespace() -> None:
    text = subprocess.run([sys.executable, str(ROOT / "scripts/experiments/render_mcp.py"), "baseline", "--mcp-image", MCP,
                           "--probe-image", PROBE, "--release-sha", "s"], capture_output=True, text=True, check=True).stdout
    documents = list(yaml.safe_load_all(text))
    assert documents[0]["kind"] == "Namespace"
    assert all(item["metadata"]["namespace"] == "ewallet-mcp-exp" for item in documents[1:])
    assert {"ClusterRole", "ClusterRoleBinding", "Role"}.isdisjoint(item["kind"] for item in documents)
    assert "with_failover" in next(item for item in documents if item["kind"] == "ConfigMap")["data"]["mcp_probe.py"]


def test_production_change_fails_the_run(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    wrapper = tmp_path / "bin"
    wrapper.mkdir()
    (wrapper / "kubectl").write_text(
        f'#!/usr/bin/env bash\ncase "$*" in *" delete pod "*) echo 2 > "{state}/prod";; esac\nexec "{SIM}/kubectl" "$@"\n',
        encoding="utf-8")
    (wrapper / "kubectl").chmod(0o755)
    result, _, _ = session(tmp_path, PATH=f"{wrapper}:{SIM}:{os.environ['PATH']}")
    assert result.returncode != 0
    assert "PRODUCTION STATE CHANGED" in result.stderr


@pytest.mark.parametrize(("env", "message"), [
    ({"EXP_APPROVED": ""}, "EXP_APPROVED"), ({"KUBE_CONTEXT": "other"}, "KUBE_CONTEXT"),
    ({"MCP_IMAGE": "repo/mcp:latest"}, "digest"), ({"EXP_FAILURE_AFTER": "99"}, "inside the run"),
])
def test_refusals_change_nothing(tmp_path, env, message) -> None:
    result, _, state = session(tmp_path, **env)
    assert result.returncode != 0 and message in result.stderr
    assert not (state / "namespace").exists() and not (state / "deleted").exists()


def test_existing_namespace_stops_the_run(tmp_path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state/namespace").write_text("true", encoding="utf-8")
    result, _, state = session(tmp_path)
    assert result.returncode != 0 and "already exists" in result.stderr
    assert not (state / "deleted").exists()


def test_failed_cleanup_is_reported(tmp_path) -> None:
    result, _, _ = session(tmp_path, SIM_DELETE_FAILS="yes")
    assert result.returncode != 0 and "CLEANUP FAILED" in result.stderr


def test_arms_compare_on_the_same_controls(tmp_path) -> None:
    from ops.experiments.adapters import load_arm
    from ops.experiments.compare import IMPROVED, compare
    from ops.experiments.spec import load_spec

    for _ in range(2):
        session(tmp_path, "baseline", SIM_FAILS="1")
        session(tmp_path, "optimized")
    spec = load_spec(ROOT / "configs/experiments/exp-c5-mcp-failover.json")
    evidence = tmp_path / "evidence"
    report = compare(spec, load_arm(evidence / "baseline"), load_arm(evidence / "optimized"))
    assert report["verdict"] == IMPROVED, report["reasons"]


class Flaky(BaseHTTPRequestHandler):
    """Answers 503 a fixed number of times, then 200; counts every call."""

    failures = 0
    status = 503
    calls = 0

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        cls = type(self)
        self.rfile.read(int(self.headers["content-length"]))
        cls.calls += 1
        code = cls.status if cls.calls <= cls.failures else 200
        self.send_response(code)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def probe_once(failures: int, status: int, attempts: int) -> tuple[dict, int]:
    handler = type("Handler", (Flaky,), {"failures": failures, "status": status, "calls": 0})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def go() -> dict:
        async with httpx.AsyncClient(timeout=5) as client:
            send = mcp_probe.http_sender(client, f"http://127.0.0.1:{server.server_port}/mcp", "token")
            policy = FailoverPolicy(max_attempts=attempts, base_delay_seconds=0.0, max_delay_seconds=0.0)
            return await mcp_probe.one_call(send, policy, 1)

    try:
        return asyncio.run(go()), handler.calls
    finally:
        server.shutdown()


def test_real_probe_recovers_from_a_503_with_retry() -> None:
    row, count = probe_once(1, 503, 3)
    assert row["ok"] is True and row["retries"] == 1 and count == 2


def test_real_probe_fails_on_a_503_without_retry() -> None:
    row, count = probe_once(1, 503, 1)
    assert row["ok"] is False and count == 1


def test_real_probe_never_repeats_a_rejected_call() -> None:
    row, count = probe_once(5, 401, 3)
    assert row["ok"] is False and count == 1


def test_real_probe_stops_at_the_attempt_budget() -> None:
    row, count = probe_once(9, 503, 3)
    assert row["ok"] is False and count == 3
