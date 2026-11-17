"""Per-replica MCP credential check: the harness must fail whenever a required probe fails.

The real scripts/llm_agent/mcp_credential_check.sh runs end to end against fake kubectl, ss, curl and probe
binaries (no cluster). Assertions are on the exit code and the verdict file, not on printed text alone.
Stdlib only: run with `uv run --frozen --project infra/docker/g3-byo-coordinator --group test python -m pytest`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import textwrap

import pytest

from ops.llm_agent import mcp_credential_verdict as v

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/llm_agent/mcp_credential_check.sh"

FAKE_KUBECTL = r'''#!/usr/bin/env python3
import base64, json, os, sys, time
from pathlib import Path
scenario = json.loads(Path(os.environ["FAKE_SCENARIO"]).read_text())
state = Path(os.environ["FAKE_STATE"])
args = sys.argv[1:]
while args and (args[0].startswith("--context") or args[0].startswith("--request-timeout")):
    args = args[2:] if args[0] == "--context" else args[1:]
ns = ""
if "-n" in args:
    i = args.index("-n"); ns = args[i + 1]; args = args[:i] + args[i + 2:]
if "port-forward" in args:
    pod = [a for a in args if a.startswith("pod/")][0][4:]
    port = [a for a in args if a.endswith(":8080")][0].split(":")[0]
    (state / f"port-{port}").write_text(str(os.getpid()))
    (state / f"pod-{port}").write_text(pod)
    time.sleep(600)
    sys.exit(0)
verb, kind = args[0], args[1]
name = args[2] if len(args) > 2 and not args[2].startswith("-") else None
def out(text): sys.stdout.write(text); sys.exit(0)
if kind == "secret":
    if name in scenario.get("missing_secrets", []): sys.exit("NotFound")
    out(base64.b64encode(f"tok-{name}".encode()).decode())
if kind == "remotemcpservers.kagent.dev":
    out(f"mcp-token-kagent-controller-{name}-r1-release1")
if kind == "deploy":
    d = scenario["servers"].get(f"{ns}/{name}")
    if d is None: sys.exit("NotFound")
    out(json.dumps({"spec": {"replicas": d["spec"]}, "status": {"readyReplicas": d.get("ready", d["spec"])}}))
if kind == "pods":
    label = args[args.index("-l") + 1].split("=", 1)[1]
    d = scenario["servers"].get(f"{ns}/{label}") or {}
    pods = d.get("pods", [])
    if "json" in args:
        items = [{"metadata": {"name": p}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}} for p in pods]
        out(json.dumps({"items": items}))
    out("".join(f"pod/{p}\n" for p in pods))
present = scenario.get("legacy_present_kinds", [])
out(f"{kind}/{name}\n" if kind in present else "")
'''

FAKE_SS = r'''#!/usr/bin/env python3
import os, re, sys
from pathlib import Path
port = re.search(r":(\d+)", " ".join(sys.argv[1:])).group(1)
f = Path(os.environ["FAKE_STATE"]) / f"port-{port}"
if f.exists():
    pid = f.read_text().strip()
    alive = Path(f"/proc/{pid}").exists()
    if alive: print(f'LISTEN 0 128 127.0.0.1:{port} 0.0.0.0:* users:(("kubectl",pid={pid},fd=3))')
'''

FAKE_CURL = "#!/usr/bin/env bash\nprintf 200\n"

FAKE_PROBE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert args[0] == "mcp"
opt = lambda k: args[args.index(k) + 1] if k in args else None
port = opt("--endpoint").split(":")[2].split("/")[0]
state = Path(os.environ["FAKE_STATE"])
pod = (state / f"pod-{port}").read_text()
token = os.environ["EWALLET_MCP_SERVICE_TOKEN"]
control = os.environ.get("AGI_CONTROL_TOKEN", "")
who = "invalid" if token.startswith("invalid-") else ("controller" if "kagent-controller" in token else "active")
tool = opt("--tool")
scenario = json.loads(Path(os.environ["FAKE_SCENARIO"]).read_text())
override = next((o for o in scenario.get("overrides", []) if o["pod"] == pod and o["who"] == who), {})
if override.get("no_evidence"):
    sys.exit(1)
report = {"tool": tool, "expect": opt("--expect"), "endpoint": opt("--endpoint")}
if who == "active":
    report.update(stage="call", tools=[tool], outcome="ok", http_statuses=[200])
elif who == "controller":
    report.update(stage="call", tools=[tool], outcome="hidden_tool", http_statuses=[200])
else:
    report.update(stage="initialize", tools=None, outcome="unauthenticated", http_statuses=[401])
report.update({k: val for k, val in override.items() if k not in ("pod", "who", "control")})
if control:
    report["control"] = override.get("control", {"outcome": "listed", "tools": [tool], "http_statuses": [200]})
Path(opt("--evidence")).write_text(json.dumps(report))
sys.exit(0)
'''


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


BASE = {
    "servers": {
        "ewallet-mcp/feature-rag-mcp": {"spec": 2, "pods": ["feature-rag-mcp-a", "feature-rag-mcp-b"]},
        "ewallet-mcp/drift-mcp": {"spec": 2, "pods": ["drift-mcp-a", "drift-mcp-b"]},
    },
}


def run(tmp_path: Path, scenario: dict, *, legacy: str = "absent", held_ports: dict | None = None):
    fakes, state, ev = tmp_path / "bin", tmp_path / "state", tmp_path / "ev"
    for d in (fakes, state, ev / "inputs"):
        d.mkdir(parents=True)
    _exe(fakes / "kubectl", FAKE_KUBECTL)
    _exe(fakes / "ss", FAKE_SS)
    _exe(fakes / "curl", FAKE_CURL)
    _exe(fakes / "probe", FAKE_PROBE)
    (ev / "inputs/transaction.json").write_text('{"transaction_id": "t1"}')
    env_file = tmp_path / "campaign.env"
    env_file.write_text(f"CTX=ctx-test\nEV={ev}\nBASELINE=bl-test\n")
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(json.dumps(scenario))
    sleeper = None
    if held_ports:   # a stale forward from an earlier run holds the port
        sleeper = subprocess.Popen(["sleep", "60"])
        for port in held_ports:
            (state / f"port-{port}").write_text(str(sleeper.pid))
    env = dict(os.environ, AGI_ENV=str(env_file), FAKE_SCENARIO=str(scenario_file), FAKE_STATE=str(state),
               KUBECTL=str(fakes / "kubectl"), SS=str(fakes / "ss"), CURL=str(fakes / "curl"),
               MCP_PROBE=str(fakes / "probe"), MCP_PYTHON=sys.executable, LEGACY_SERVER=legacy, MCP_MIN_REPLICAS="2")
    try:
        proc = subprocess.run(["bash", str(SCRIPT), str(tmp_path / "out"), "run"], env=env, capture_output=True,
                              text=True, timeout=180)
    finally:
        if sleeper:
            sleeper.kill()
    verdict_path = tmp_path / "out/run/verdict.json"
    verdict = json.loads(verdict_path.read_text()) if verdict_path.exists() else None
    return proc, verdict


def scenario(**extra) -> dict:
    data = json.loads(json.dumps(BASE))
    data.update(extra)
    return data


def failing_reasons(verdict: dict) -> list[str]:
    reasons = [r for s in verdict["servers"] for r in s["reasons"]]
    reasons += [r for s in verdict["servers"] for c in s["checks"] for r in c["reasons"]]
    return reasons + verdict["fatal"]


def test_all_probes_pass_exit_zero_and_no_credential_in_output(tmp_path):
    proc, verdict = run(tmp_path, scenario())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert verdict["verdict"] == "PASS"
    checks = [c for s in verdict["servers"] for c in s["checks"]]
    assert len(checks) == 12 and all(c["verdict"] == "PASS" for c in checks)
    absent = next(s for s in verdict["servers"] if s["server"] == "ewallet-agents/drift-mcp")
    assert absent["mode"] == "absent" and absent["verdict"] == "PASS"
    written = proc.stdout + proc.stderr + "".join(p.read_text() for p in (tmp_path / "out").rglob("*") if p.is_file())
    assert "tok-" not in written and "invalid-" not in written, "credential values must never be written"


def test_one_failed_probe_makes_the_run_fail(tmp_path):
    proc, verdict = run(tmp_path, scenario(overrides=[{"pod": "drift-mcp-b", "who": "active", "outcome": "error"}]))
    assert proc.returncode != 0
    assert verdict["verdict"] == "FAIL"
    assert any("business execution failed" in r for r in failing_reasons(verdict))


def test_unexpected_authorization_success_fails(tmp_path):
    proc, verdict = run(tmp_path, scenario(overrides=[{"pod": "feature-rag-mcp-a", "who": "controller", "outcome": "ok"}]))
    assert proc.returncode != 0
    assert any("unexpected authorization success" in r for r in failing_reasons(verdict))


def test_invalid_credential_accepted_fails(tmp_path):
    proc, verdict = run(tmp_path, scenario(overrides=[{"pod": "drift-mcp-a", "who": "invalid", "stage": "call",
                                                       "tools": ["drift_detection"], "outcome": "ok"}]))
    assert proc.returncode != 0
    assert any("unexpected authentication success" in r for r in failing_reasons(verdict))


def test_missing_evidence_fails(tmp_path):
    proc, verdict = run(tmp_path, scenario(overrides=[{"pod": "feature-rag-mcp-b", "who": "invalid", "no_evidence": True}]))
    assert proc.returncode != 0
    assert any("missing or malformed probe evidence" in r for r in failing_reasons(verdict))


def test_missing_expected_replica_fails(tmp_path):
    data = scenario()
    data["servers"]["ewallet-mcp/drift-mcp"] = {"spec": 2, "ready": 1, "pods": ["drift-mcp-a"]}
    proc, verdict = run(tmp_path, data)
    assert proc.returncode != 0
    assert any("expected 2 Ready replicas" in r for r in failing_reasons(verdict))


def test_replicas_below_the_expected_minimum_fail(tmp_path):
    data = scenario()
    data["servers"]["ewallet-mcp/feature-rag-mcp"] = {"spec": 1, "pods": ["feature-rag-mcp-a"]}
    proc, verdict = run(tmp_path, data)
    assert proc.returncode != 0
    assert any("below the expected minimum 2" in r for r in failing_reasons(verdict))


def test_missing_server_deployment_fails(tmp_path):
    data = scenario()
    del data["servers"]["ewallet-mcp/feature-rag-mcp"]
    proc, verdict = run(tmp_path, data)
    assert proc.returncode != 0 and verdict["verdict"] == "FAIL"


def test_stale_port_forward_is_refused_and_fails(tmp_path):
    proc, verdict = run(tmp_path, scenario(), held_ports=[18191])
    assert proc.returncode != 0
    assert any("stale forward refused" in r for r in failing_reasons(verdict))


def test_authorized_control_unavailable_fails(tmp_path):
    proc, verdict = run(tmp_path, scenario(overrides=[{"pod": "drift-mcp-a", "who": "invalid",
                                                       "control": {"outcome": "server_error", "tools": None}}]))
    assert proc.returncode != 0
    assert any("authorized control unavailable" in r for r in failing_reasons(verdict))


def test_missing_active_credential_secret_fails(tmp_path):
    proc, verdict = run(tmp_path, scenario(missing_secrets=["mcp-token-sandbox-drift-drift-mcp-sbx-20261115"]))
    assert proc.returncode != 0
    assert any("cannot read the credential Secret" in r for r in failing_reasons(verdict))


def test_retired_legacy_server_absence_is_verified(tmp_path):
    proc, verdict = run(tmp_path, scenario(legacy_present_kinds=["serviceaccount"]))
    assert proc.returncode != 0
    assert any("serviceaccount still present" in r for r in failing_reasons(verdict))


def test_legacy_present_mode_reports_backend_unavailable_without_calling_it_success(tmp_path):
    data = scenario(overrides=[{"pod": "drift-mcp-legacy", "who": "active", "outcome": "unavailable",
                                "tool_error_code": "drift_baseline_unavailable"}])
    data["servers"]["ewallet-agents/drift-mcp"] = {"spec": 1, "pods": ["drift-mcp-legacy"]}
    proc, verdict = run(tmp_path, data, legacy="present")
    legacy = next(s for s in verdict["servers"] if s["server"] == "ewallet-agents/drift-mcp")
    active = next(c for c in legacy["checks"] if c["check"] == "active")
    assert active["layers"] == {"authentication": "pass", "authorization": "granted", "execution": "backend_unavailable"}
    assert active["verdict"] == "PASS", "authentication and authorization are proven; execution is reported, not claimed"
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ------------------------------------------------------------------ decision logic (no subprocess)

def ev(**kw):
    base = {"tool": "drift_detection", "stage": "call", "tools": ["drift_detection"], "outcome": "ok"}
    base.update(kw)
    return base


CONTROL = {"outcome": "listed", "tools": ["drift_detection"]}


@pytest.mark.parametrize("evidence,layers", [
    (ev(), ("pass", "granted", "ok")),
    (ev(outcome="unavailable"), ("pass", "granted", "backend_unavailable")),
    (ev(outcome="rejected"), ("pass", "granted", "failed")),
    (ev(outcome="hidden_tool"), ("pass", "denied", "n/a")),
    (ev(outcome="unauthenticated", stage="initialize", tools=None), ("rejected", "n/a", "n/a")),
    (ev(outcome="transport_error", stage="connect", tools=None), ("unknown", "n/a", "n/a")),
    (ev(outcome="server_error", stage="initialize", tools=None), ("unknown", "n/a", "n/a")),
])
def test_layers_are_classified_separately(evidence, layers):
    assert tuple(v.asdict(v.classify(evidence)).values()) == layers


def test_backend_unavailable_fails_a_required_business_check_but_not_the_credential_layers():
    verdict, layers, reasons = v.evaluate_probe("active", ev(outcome="unavailable", tool_error_code="drift_baseline_unavailable"),
                                                tool="drift_detection", execution_required=True)
    assert verdict == "FAIL" and layers.authentication == "pass" and layers.authorization == "granted"
    assert reasons == ["business execution backend_unavailable (tool_error_code drift_baseline_unavailable)"]


def test_negative_checks_need_the_control_and_the_exact_mechanism():
    ok = v.evaluate_probe("discovery", ev(outcome="hidden_tool", control=CONTROL), tool="drift_detection", execution_required=False)
    assert ok[0] == "PASS"
    unlisted = v.evaluate_probe("discovery", ev(outcome="hidden_tool", tools=[], control=CONTROL), tool="drift_detection",
                                execution_required=False)
    assert unlisted[0] == "FAIL", "Unknown tool for an unlisted tool is not an authorization denial"
    down = v.evaluate_probe("invalid", ev(outcome="server_error", stage="initialize", tools=None, control=CONTROL),
                            tool="drift_detection", execution_required=False)
    assert down[0] == "FAIL", "a 5xx is not a credential rejection"
    wrong_tool = v.evaluate_probe("active", ev(tool="feature_lookup"), tool="drift_detection", execution_required=True)
    assert wrong_tool[0] == "FAIL"


def test_evaluate_requires_every_declared_server_and_no_fatal():
    assert v.evaluate([], [], 1)["verdict"] == "FAIL", "nothing declared is not a pass"
    records = [{"type": "absent", "namespace": "ns", "server": "x", "kind": k, "present": False} for k in v.ABSENT_KINDS]
    assert v.evaluate([("ns/x", "absent")], records, 1)["verdict"] == "PASS"
    assert v.evaluate([("ns/x", "absent")], records + [{"type": "fatal", "reason": "api"}], 1)["verdict"] == "FAIL"
    assert v.evaluate([("ns/x", "absent")], records[:-1], 1)["verdict"] == "FAIL", "an unchecked kind is not absence"


def test_malformed_records_file_fails(tmp_path):
    records = tmp_path / "r.jsonl"
    records.write_text("{not json\n")
    assert v.main(["--records", str(records), "--out", str(tmp_path / "v.json"), "--server", "ns/x=absent"]) == 1
