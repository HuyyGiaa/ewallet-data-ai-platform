"""EXP-C5-SANDBOX manifests, driver and summary.

The manifest tests encode rules read from kagent v0.10.3, because schema validation accepts
manifests that the controller then refuses: a BYO sandbox agent needs an explicit command,
serves on port 80, and gets no image entrypoint, working directory or PATH.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest
import yaml

from ops.experiments import sandbox_client, sandbox_summary

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "infra/kubernetes/agent-sandbox"
DRIVER = ROOT / "scripts/experiments/sandbox_experiment.sh"
SIM = ROOT / "tests/experiments/sim"
CONTEXT = "gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev"
IMAGE = "repo/agent@sha256:" + "c" * 64
TEMPLATE = (ROOT / "configs/experiments/fixtures/agent-load-request.json").read_text(encoding="utf-8")


def arm(name: str) -> dict:
    text = (SANDBOX / f"arm-{name}.yaml.tmpl").read_text(encoding="utf-8").replace("__AGENT_IMAGE__", IMAGE)
    return next(item for item in yaml.safe_load_all(text) if item["kind"] in {"Agent", "SandboxAgent"})


def env_of(item: dict) -> dict:
    return {entry["name"]: entry for entry in item["spec"]["byo"]["deployment"]["env"]}


def test_candidate_has_an_explicit_command() -> None:
    deployment = arm("optimized")["spec"]["byo"]["deployment"]
    assert deployment["cmd"].startswith("/")
    assert deployment["args"][0] == "-m"


def test_candidate_serves_where_substrate_probes_and_finds_its_code() -> None:
    env = env_of(arm("optimized"))
    assert env["EWALLET_AGENT_PORT"]["value"] == "80"
    assert env["PYTHONPATH"]["value"] == "/workspace"


def test_arms_differ_only_where_the_runtime_demands() -> None:
    base, candidate = env_of(arm("baseline")), env_of(arm("optimized"))
    assert set(candidate) - set(base) == {"EWALLET_AGENT_PORT", "PYTHONPATH", "PATH"}
    assert set(base) - set(candidate) == set()
    assert all(base[name] == candidate[name] for name in base)
    assert arm("baseline")["spec"]["byo"]["deployment"]["image"] == arm("optimized")["spec"]["byo"]["deployment"]["image"]


@pytest.mark.parametrize("name", ["baseline", "optimized"])
def test_arm_uses_no_production_secret(name) -> None:
    item = arm(name)
    assert item["metadata"]["namespace"] == "ewallet-sandbox"
    refs = [entry["valueFrom"]["secretKeyRef"]["name"] for entry in env_of(item).values() if "valueFrom" in entry]
    assert sorted(refs) == ["sandbox-llm-key", "sandbox-mcp-token"]
    for entry in env_of(item).values():
        if "value" in entry:
            assert not any(word in entry["name"] for word in ("TOKEN", "API_KEY", "PASSWORD"))


@pytest.mark.parametrize("name", ["baseline", "optimized"])
def test_arm_validates_against_the_pinned_crd(name) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schemas = json.loads((ROOT / "infra/kubernetes/kagent-platform/crd-schemas-kagent-0.10.3.json").read_text(encoding="utf-8"))
    item = arm(name)
    schema = schemas["SandboxAgent" if item["kind"] == "SandboxAgent" else "Agent"]["schema"]["properties"]["spec"]
    jsonschema.validate(item["spec"], schema)


def test_pool_identity_is_dedicated_and_has_no_token() -> None:
    documents = list(yaml.safe_load_all((SANDBOX / "namespace.yaml").read_text(encoding="utf-8")))
    account = next(item for item in documents if item["kind"] == "ServiceAccount")
    pool = next(item for item in documents if item["kind"] == "WorkerPool")
    assert account["automountServiceAccountToken"] is False
    assert pool["metadata"]["namespace"] == "ewallet-sandbox"
    assert "@sha256:" in pool["spec"]["ateomImage"]
    assert not {"Role", "RoleBinding", "ClusterRoleBinding"} & {item["kind"] for item in documents}


def test_candidate_uses_the_dedicated_pool() -> None:
    assert arm("optimized")["spec"]["substrate"]["workerPoolRef"]["name"] == "business-agents"


def test_s1_rules_admit_only_the_dedicated_identities() -> None:
    documents = list(yaml.safe_load_all((SANDBOX / "s1-access.yaml").read_text(encoding="utf-8")))
    for item in documents:
        if item["kind"] == "AuthorizationPolicy":
            principals = item["spec"]["rules"][0]["from"][0]["source"]["principals"]
            assert all(value.startswith("cluster.local/ns/ewallet-sandbox/sa/") for value in principals)
    assert "kagent-pilot" not in (SANDBOX / "s1-access.yaml").read_text(encoding="utf-8")


def test_real_specialist_boots_with_the_candidate_command_and_env() -> None:
    pytest.importorskip("a2a")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    item = arm("optimized")
    deployment = item["spec"]["byo"]["deployment"]
    env = {entry["name"]: entry.get("value", "x" * 40) for entry in deployment["env"]}
    env.update({"EWALLET_AGENT_PORT": str(port), "PYTHONPATH": str(ROOT), "PATH": os.environ["PATH"], "KAGENT_NAME": "x",
                "KAGENT_NAMESPACE": "x", "HOME": "/tmp", "EWALLET_OTEL_LOGS_ENABLED": "false",
                "EWALLET_OTEL_METRICS_ENABLED": "false"})
    process = subprocess.Popen([sys.executable, *deployment["args"]], env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, cwd=ROOT)
    try:
        card = None
        for _ in range(100):
            try:
                with urlopen(f"http://127.0.0.1:{port}/.well-known/agent-card.json", timeout=1) as response:
                    card = json.loads(response.read())
                    break
            except OSError:
                assert process.poll() is None, process.stdout.read()
                time.sleep(0.2)
        assert card and card["name"]
    finally:
        process.terminate()
        process.wait(timeout=10)


def session(tmp_path: Path, name: str = "optimized", action: str = "run", **env: str):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    evidence = tmp_path / "evidence"
    server = subprocess.Popen([sys.executable, str(SIM / "sandbox_client_stub.py"), "18383"],
                              env={**os.environ, **{k: v for k, v in env.items() if k == "SIM_AGENT_FAILS"}})
    full = {**os.environ, "PATH": f"{SIM / 'sandbox_bin'}:{os.environ['PATH']}", "SIM_STATE": str(state),
            "KUBE_CONTEXT": CONTEXT, "AGENT_IMAGE": IMAGE, "EXP_APPROVED": "yes", "EXP_PYTHON": sys.executable,
            "EXP_FORWARD_WAIT": "1", "EXP_TASKS": "2", "EXP_INIT_LIMIT": "30", **env}
    try:
        # The stub listens on the forwarded port, so the driver's in-use check is skipped here.
        wrapper = tmp_path / "bin"
        wrapper.mkdir(exist_ok=True)
        (wrapper / "ss").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (wrapper / "ss").chmod(0o755)
        full["PATH"] = f"{wrapper}:{full['PATH']}"
        for _ in range(50):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", 18383)) == 0:
                    break
            time.sleep(0.05)
        result = subprocess.run([str(DRIVER), name, action, str(evidence)], capture_output=True, text=True, env=full,
                                timeout=60, check=False)
    finally:
        server.terminate()
        server.wait(timeout=5)
    return result, evidence, state


@pytest.mark.parametrize("name", ["baseline", "optimized"])
def test_full_run_measures_and_removes_the_arm(tmp_path, name) -> None:
    result, evidence, state = session(tmp_path, name)
    assert result.returncode == 0, result.stderr
    level = json.loads((evidence / f"{name}/raw/run-01/summary.json").read_text(encoding="utf-8"))["levels"]["load"]
    assert level["status"] == "MEASURED"
    assert level["requests"] == 5 and level["succeeded"] == 5
    assert level["initialization_seconds"] is not None and level["first_success_seconds"] is not None
    assert level["successful_mcp_calls"] == 5 * sandbox_summary.tools_per_task(TEMPLATE)
    assert level["worker_cpu_limit_percent"] == pytest.approx(40)
    assert not (state / "arm").exists()


def test_recovery_is_opt_in_and_stays_in_the_namespace(tmp_path) -> None:
    result, evidence, state = session(tmp_path, EXP_RECOVERY="yes")
    assert result.returncode == 0, result.stderr
    assert "-n ewallet-sandbox" in (state / "deleted").read_text(encoding="utf-8")
    level = json.loads((evidence / "optimized/raw/run-01/summary.json").read_text(encoding="utf-8"))["levels"]["load"]
    assert level["recovery_seconds"] is not None


def test_no_pod_is_deleted_without_opt_in(tmp_path) -> None:
    _, _, state = session(tmp_path)
    assert not (state / "deleted").exists()


def test_boot_failure_is_a_result_with_cleanup(tmp_path) -> None:
    result, evidence, state = session(tmp_path, SIM_NEVER_READY="yes")
    assert result.returncode == 0, result.stderr
    level = json.loads((evidence / "optimized/raw/run-01/summary.json").read_text(encoding="utf-8"))["levels"]["load"]
    assert level["status"] == "NEVER_ANSWERED"
    assert "not Ready" in (evidence / "manifest.json").read_text(encoding="utf-8")
    assert not (state / "arm").exists()


@pytest.mark.parametrize(("env", "message"), [
    ({"EXP_APPROVED": ""}, "EXP_APPROVED"), ({"KUBE_CONTEXT": "other"}, "KUBE_CONTEXT"),
    ({"AGENT_IMAGE": "repo/agent:latest"}, "digest"), ({"SIM_NS_MARK": ""}, "not ready"),
    ({"SIM_NO_SECRET": "sandbox-mcp-token"}, "not ready"), ({"SIM_WORKERS": "0"}, "not ready"),
])
def test_refusals_create_nothing(tmp_path, env, message) -> None:
    result, _, state = session(tmp_path, **env)
    assert result.returncode != 0 and message in result.stderr
    assert not (state / "arm").exists()


def test_driver_never_creates_namespace_pool_or_secret() -> None:
    text = DRIVER.read_text(encoding="utf-8")
    assert "create secret" not in text and "delete namespace" not in text and "delete workerpool" not in text
    assert "kagent-pilot" not in text and "ewallet-agents" not in text


def row(phase: str, finished: float, ok: bool = True) -> dict:
    return {"phase": phase, "started": finished - 1, "finished": finished, "ok": ok, "e2e_ms": 1000.0}


def test_summary_keeps_failed_attempts_and_separates_phases() -> None:
    rows = [row("first", 50, False), row("first", 60), row("new", 70), row("resume", 80), row("recovery", 120)]
    level = sandbox_summary.summarize(rows, [], applied_at=10, ready_at=30, failure_at=100, tools=3, workload_sha256="w",
                                      arm_prefix="x")["levels"]["load"]
    assert level["requests"] == 5 and level["succeeded"] == 4
    assert level["initialization_seconds"] == 20 and level["first_success_seconds"] == 50
    assert level["recovery_seconds"] == 20 and level["successful_mcp_calls"] == 12


def test_agent_that_never_answers_is_not_measured() -> None:
    level = sandbox_summary.summarize([row("first", 50, False)], [], applied_at=10, ready_at=None, failure_at=None, tools=3,
                                      workload_sha256="w", arm_prefix="x")["levels"]["load"]
    assert level["status"] == "NEVER_ANSWERED" and level["initialization_seconds"] is None


def test_first_phase_retries_with_new_sessions_until_success() -> None:
    seen, lines = [], []
    replies = iter(["failed", "failed", "completed"])

    def post(message, timeout):
        seen.append(message["params"]["message"]["contextId"])
        return {"result": {"id": "r", "status": {"state": next(replies)}}}

    assert sandbox_client.wait_for_success(post, TEMPLATE, "first", "r", limit=30, timeout=1, pause=0, emit=lines.append,
                                           sleep=lambda _: None)
    assert len(seen) == 3 == len(set(seen)) == len(lines)


def test_resume_phase_stays_in_one_session() -> None:
    contexts: dict[str, list[str]] = {"new": [], "resume": []}

    def post(message, timeout):
        task = message["params"]["message"]["messageId"]
        for phase, values in contexts.items():
            if f"-{phase}-" in task:
                values.append(message["params"]["message"]["contextId"])
        return {"result": {"id": "r", "status": {"state": "completed"}}}

    assert sandbox_client.run_phases(post, TEMPLATE, "r", tasks=3, limit=5, timeout=1, emit=lambda _: None)
    assert len(set(contexts["resume"])) == 1 and len(set(contexts["new"])) == 3
