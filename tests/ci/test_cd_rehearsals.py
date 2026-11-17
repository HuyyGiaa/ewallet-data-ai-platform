"""Rehearsal manifests stay faithful to the frozen production runtime they stand in for.

* agent-fleet: each Specialist starts with exactly its SandboxAgent actor contract
  (infra/kubernetes/agent-sandbox/fleet.yaml.tmpl);
* mcp-servers: each server runs its production factory (chart values) and the smoke expects the
  tool set that server really serves;
* coordinator: the Sandbox-only routing configuration of the production chart.

The boot tests start the real application factories offline with the rehearsal configuration
(no cluster, no backend): they are skipped, and say so, when the locked environment running the
suite lacks the application's dependencies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "infra/kubernetes/cicd-runtime"


def documents(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def by_name(items: list[dict], kind: str) -> dict[str, dict]:
    return {item["metadata"]["name"]: item for item in items if item["kind"] == kind}


def fleet_template() -> dict[str, dict]:
    text = (ROOT / "infra/kubernetes/agent-sandbox/fleet.yaml.tmpl").read_text().replace("__", "x")
    return {item["metadata"]["name"]: item["spec"]["byo"]["deployment"]
            for item in yaml.safe_load_all(text) if item and item["kind"] == "SandboxAgent"}


CONTRACT_ENV = ("EWALLET_AGENT_PORT", "PYTHONPATH", "PATH", "PYTHONUNBUFFERED", "EWALLET_NL_INTAKE_ENABLED",
                "EWALLET_MCP_MAX_ATTEMPTS")


def test_fleet_rehearsal_reproduces_each_sandbox_actor_contract() -> None:
    template = fleet_template()
    rehearsal = by_name(documents(RUNTIME / "agents/agent-fleet-cd.yaml"), "Deployment")
    assert set(template) == {"feature-rag-specialist", "drift-specialist"}
    for agent, actor in template.items():
        container = rehearsal[f"fleet-cd-{agent}"]["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == [actor["cmd"]], agent
        assert container["args"] == actor["args"], agent
        production_env = {item["name"]: item.get("value") for item in actor["env"] if "value" in item}
        rehearsal_env = {item["name"]: item.get("value") for item in container["env"]}
        for name in CONTRACT_ENV:
            assert rehearsal_env.get(name) == production_env.get(name), (agent, name)
        assert container["ports"][0]["containerPort"] == int(production_env["EWALLET_AGENT_PORT"]) == 80
        assert container["resources"] == actor["resources"], agent
        assert container["image"] == "__IMAGE__"


def test_fleet_smoke_targets_both_specialists_by_their_card_names() -> None:
    job = by_name(documents(RUNTIME / "agents/agent-fleet-cd.yaml"), "Job")["fleet-cd-smoke"]
    command = job["spec"]["template"]["spec"]["containers"][0]["command"]
    targets = [command[index + 1] for index, item in enumerate(command) if item == "--target"]
    assert targets == ["feature_rag_specialist=http://fleet-cd-feature-rag-specialist:80",
                       "drift_specialist=http://fleet-cd-drift-specialist:80"]
    contract = json.loads((ROOT / "jenkins/cd/agent-fleet.contract.json").read_text())
    assert {"feature_rag_specialist", "drift_specialist"} <= set(contract["expected"])


def test_mcp_rehearsal_runs_each_production_factory() -> None:
    values = yaml.safe_load((ROOT / "infra/helm/llm-agent-platform/values.yaml").read_text())
    services = values["mcpServices"]["services"]
    rehearsal = by_name(documents(RUNTIME / "mcp/mcp-cd.yaml"), "Deployment")
    for service, deployment in (("feature-rag-mcp", "mcp-cd-feature-rag"), ("drift-mcp", "mcp-cd-drift")):
        command = rehearsal[deployment]["spec"]["template"]["spec"]["containers"][0]["command"]
        assert command[command.index("--factory") + 1] == services[service]["app"], service


def mcp_smoke_tools() -> dict[str, list[str]]:
    job = by_name(documents(RUNTIME / "mcp/mcp-cd.yaml"), "Job")["mcp-cd-smoke"]
    command = job["spec"]["template"]["spec"]["containers"][0]["command"]
    pairs = [command[index + 1].split("=", 1) for index, item in enumerate(command) if item == "--tool"]
    return {name: tools.split(",") for name, tools in pairs}


def test_mcp_grants_match_the_smoke_tool_sets() -> None:
    config = by_name(documents(RUNTIME / "mcp/mcp-cd.yaml"), "ConfigMap")
    tools = mcp_smoke_tools()
    grants = {name: json.loads(config[f"mcp-cd-auth-{name.removesuffix('-mcp')}"]["data"]["EWALLET_MCP_TOOL_GRANTS"])
              for name in tools}
    for name, expected in tools.items():
        assert sorted(grants[name]["cd-smoke"]) == sorted(expected), name


def test_coordinator_rehearsal_uses_the_sandbox_only_routing() -> None:
    env = by_name(documents(RUNTIME / "agents/coordinator-cd.yaml"), "ConfigMap")["coordinator-cd-env"]["data"]
    overlay = yaml.safe_load((ROOT / "infra/helm/llm-agent-platform/agi/specialists-sandbox-only.yaml").read_text())
    sandbox = sorted(name for name, spec in overlay["agents"]["specialists"].items() if spec["runtime"] == "sandbox")
    assert env["EWALLET_SANDBOX_SPECIALISTS"] == ",".join(sandbox)
    assert env["EWALLET_SANDBOX_NAMESPACE"] == "ewallet-sandbox"
    assert env["EWALLET_COORDINATOR_WORKFLOWS"] == "fraud,drift"


def test_no_rehearsal_deploys_a_legacy_byo_specialist_deployment() -> None:
    for path in RUNTIME.rglob("*.yaml"):
        text = path.read_text()
        assert "agent-cd-specialist" not in text, path
        assert not re.search(r"name: (feature-rag|drift)-specialist\b", text), path


def test_cd_smoke_checks_every_mcp_server_and_reports_missing_tools(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from scripts.ci import cd_smoke

    def fake_http(url, *, body=None, headers=None):  # type: ignore[no-untyped-def]
        if url.endswith("/health/ready"):
            return 200, ""
        if not (headers or {}).get(cd_smoke.TOKEN_HEADER):
            return 401, ""
        tools = ["drift_detection"] if "drift" in url else ["feature_lookup", "fraud_scoring"]
        return 200, json.dumps({"result": {"tools": [{"name": name} for name in tools]}})

    monkeypatch.setattr(cd_smoke, "http", fake_http)
    monkeypatch.setenv("CD_SMOKE_TOKEN", "t")
    lines: list[str] = []
    monkeypatch.setattr("builtins.print", lambda line: lines.append(line))
    monkeypatch.setattr(sys, "argv", ["cd_smoke", "--mode", "mcp", "--release-sha", "s",
                                      "--target", "feature-rag-mcp=http://a", "--tool",
                                      "feature-rag-mcp=feature_lookup,fraud_scoring,knowledge_search",
                                      "--target", "drift-mcp=http://drift", "--tool", "drift-mcp=drift_detection"])
    assert cd_smoke.main() == 0
    summary = json.loads(lines[-1].removeprefix("CD_SMOKE_SUMMARY "))
    assert summary["drift-mcp"]["tools_missing"] == []
    assert summary["feature-rag-mcp"]["tools_missing"] == ["knowledge_search"]
    assert summary["feature-rag-mcp"]["anonymous_status"] == 401


# ---------------------------------------------------------------- offline boots of the real factories

def container_env(path: Path, deployment: str) -> dict[str, str]:
    items = documents(path)
    container = by_name(items, "Deployment")[deployment]["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: str(item["value"]) for item in container["env"] if "value" in item}
    configs = by_name(items, "ConfigMap")
    for source in container.get("envFrom", []):
        env.update({key: str(value) for key, value in configs[source["configMapRef"]["name"]]["data"].items()})
    return env


@pytest.mark.parametrize("deployment,service", [("mcp-cd-feature-rag", "feature-rag-mcp"), ("mcp-cd-drift", "drift-mcp")])
def test_each_mcp_factory_boots_offline_with_its_rehearsal_configuration(deployment, service, tmp_path) -> None:  # type: ignore[no-untyped-def]
    try:
        from fastapi.testclient import TestClient

        from ai_platform.llm_agent.tools import service as factories
    except ImportError as error:
        pytest.skip(f"needs the llm-agent-tooling application dependencies ({error.name})")
    if service == "feature-rag-mcp":
        pytest.importorskip("feast", reason="feature-rag-mcp needs the locked feast extra")
    token = "rehearsal-token-" + "x" * 24
    env = {key: value.replace("__SMOKE_TOKEN__", token).replace("__RELEASE_SHA__", "a" * 40)
           .replace("__IMAGE_DIGEST__", "sha256:" + "b" * 64) for key, value in
           container_env(RUNTIME / "mcp/mcp-cd.yaml", deployment).items()}
    if "EWALLET_FEAST_REGISTRY" in env:
        env["EWALLET_FEAST_REGISTRY"] = str(tmp_path / "registry.db")
    factory = getattr(factories, "create_feature_rag_app" if service == "feature-rag-mcp" else "create_drift_app")
    monkey = dict(os.environ)
    os.environ.update(env)
    try:
        client = TestClient(factory(env))
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "MCP-Protocol-Version": "2025-06-18"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        assert client.get("/health/ready").status_code == 200
        assert client.post("/mcp", json=body, headers=headers).status_code == 401
        response = client.post("/mcp", json=body, headers={**headers, "X-Ewallet-MCP-Service-Token": token})
        assert response.status_code == 200
        assert sorted(item["name"] for item in response.json()["result"]["tools"]) == sorted(mcp_smoke_tools()[service])
    finally:
        os.environ.clear()
        os.environ.update(monkey)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("deployment,card", [("fleet-cd-feature-rag-specialist", "feature_rag_specialist"),
                                             ("fleet-cd-drift-specialist", "drift_specialist")])
def test_each_specialist_boots_with_its_actor_contract_and_rejects_a_malformed_task(deployment, card) -> None:  # type: ignore[no-untyped-def]
    # The Specialists' dependencies live in the g3-byo-coordinator project, not in the tooling
    # project that runs this suite: CD_REHEARSAL_AGENT_PYTHON names that locked interpreter.
    python = os.environ.get("CD_REHEARSAL_AGENT_PYTHON", sys.executable)
    probe = subprocess.run([python, "-c", "import a2a, ai_platform.llm_agent.byo_runtime"], cwd=ROOT,
                           env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True)
    if probe.returncode != 0:
        pytest.skip("needs the g3-byo-coordinator dependencies (set CD_REHEARSAL_AGENT_PYTHON)")
    items = documents(RUNTIME / "agents/agent-fleet-cd.yaml")
    container = by_name(items, "Deployment")[deployment]["spec"]["template"]["spec"]["containers"][0]
    port = free_port()  # the actor's port 80 needs the in-cluster sysctl; the contract is the same otherwise
    env = {**os.environ, **{item["name"]: str(item["value"]) for item in container["env"]},
           "EWALLET_AGENT_PORT": str(port), "PYTHONPATH": str(ROOT), "PATH": os.environ["PATH"]}
    process = subprocess.Popen([python, *container["args"]], cwd=ROOT, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        card_body = None
        while time.monotonic() < deadline and card_body is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/.well-known/agent-card.json", timeout=2) as response:
                    card_body = json.load(response)
            except OSError:
                time.sleep(0.5)
        assert card_body is not None and card_body["name"] == card
        payload = json.dumps({"jsonrpc": "2.0", "id": "x", "method": "message/send", "params": {"message": {
            "role": "user", "messageId": "m", "parts": [{"kind": "text", "text": "CALL_AGENT=not-a-request"}]}}}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{port}/", data=payload,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            state = json.load(response)["result"]["status"]["state"]
        assert state in {"rejected", "failed"}
    finally:
        process.terminate()
        process.wait(timeout=20)
