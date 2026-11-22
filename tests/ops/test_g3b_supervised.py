"""Supervised G3-B orchestrator: complete security gates, deadline/cancellation, ACME cleanup.

Offline: the campaign runs with the Simulator (commands recorded, never executed) or real
local subprocesses (sleep) for timeouts and signals. Live gates (L4/L5 from the Internet) are
only exercised as orchestration logic here; their acceptance needs the live run."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from tests.ops.test_dns_campaign import EDGE, Recorder, camp, cli, ready_state, wait_for_phase  # noqa: F401

GRAFANA_ROWS = [{"row": r, "check": c, "passed": True, "evidence_class": "PUBLIC_INTERNET"} for r, c in (
    ("37", "Grafana appUrl is the public root URL"), ("37", "Grafana login"), ("38", "logs: Loki returns log lines"),
    ("39", "traces: Tempo returns traces"))]


class Campaign(Recorder):
    """Recorder with a scripted client address sequence and JSONL outputs for public_e2e runs."""

    def __init__(self, log, fail=(), ips=("203.0.113.7",), e2e_rows=None, plan_uncovered=("ewallet-mcp/feature-rag-mcp",)):
        super().__init__(log, fail, plan_uncovered)
        self.ips, self.dig_calls, self.e2e_rows = list(ips), 0, e2e_rows

    def run(self, cmd, env=None, timeout=1800):
        text = " ".join(cmd)
        if "myip.opendns.com" in text:
            self.cmds.append((text, []))
            ip = self.ips[min(self.dig_calls, len(self.ips) - 1)]
            self.dig_calls += 1
            return 0, ip
        rc, out = super().run(cmd, env, timeout)
        if rc == 0 and "public_e2e.py" in text and "--out" in cmd:
            rows = self.e2e_rows(text) if self.e2e_rows else []
            Path(cmd[cmd.index("--out") + 1]).write_text("".join(json.dumps(r) + "\n" for r in rows))
        return rc, out


def rows_for(text):
    """What a real public_e2e run would have written, for the agents mode in the command."""
    if "--agents denied" in text:
        return [{"row": "40", "check": "agents: refused from a non-allowed address", "passed": True,
                 "evidence_class": "PUBLIC_INTERNET" if "PUBLIC_INTERNET" in text else "GKE_PRIVATE"}]
    return GRAFANA_ROWS if "--grafana-root-url" in text else []


def run_campaign(tmp_path, ips=("203.0.113.7",), fail=(), **kw):
    path, _ = ready_state(tmp_path, **kw)
    ex = Campaign(tmp_path / "sim.jsonl", fail=fail, ips=ips, e2e_rows=rows_for)
    return camp.run(path, ex), ex


def has(ex, needle):
    return any(needle in c for c, _ in ex.cmds)


# --- A. security gates ---------------------------------------------------------------------------

@pytest.mark.parametrize("failing,phase", [
    ("ownership_preflight.py render", "L0-render"),
    ("07-private-e2e", "private-e2e"),
    ("07-private-negative", "private-e2e"),
])
def test_a_failed_l0_or_private_e2e_never_opens_the_public_lb(tmp_path, failing, phase):
    state, ex = run_campaign(tmp_path, fail=(failing,))
    assert state["phases"][phase].startswith("FAIL")
    assert not has(ex, "values-gateway-public-open.yaml") or not any(
        "upgrade ewallet-edge-gateway" in c and "public-open" in c for c, _ in ex.cmds)
    assert state["verdicts"]["G3B_SECURITY_STATUS"] == "FAIL" and state["CLEANUP_STATUS"] == "COMPLETE"


def test_l0_live_inactive_security_stops_before_the_lb(tmp_path):
    state, ex = run_campaign(tmp_path, fail=("live --context ctx --kubeconfig kc --allow-public-loadbalancer",))
    assert state["phases"]["L0-live"].startswith("FAIL")
    assert not any("upgrade ewallet-edge-gateway" in c and "public-open" in c for c, _ in ex.cmds)


def test_l0_render_uses_the_exact_public_values(tmp_path):
    _, ex = run_campaign(tmp_path)
    render = next(c for c, _ in ex.cmds if "ownership_preflight.py render" in c)
    assert "--allow-public-loadbalancer" in render and "--domain edge.example.test" in render and "--approved-cidrs-file" in render
    gw = next(c for c, _ in ex.cmds if "helm template ewallet-edge-gateway" in c)
    assert "values-gateway-public.yaml" in gw and "values-gateway-public-open.yaml" in gw
    edge = next(c for c, _ in ex.cmds if "helm template ewallet-edge " in c)
    assert "g3b-values_acme.yaml" in edge


def test_private_e2e_is_gke_private_through_a_port_forward_with_agents_denied(tmp_path):
    _, ex = run_campaign(tmp_path)
    forward = next(c for c, _ in ex.cmds if "port-forward" in c)
    assert "--address 127.0.0.1" in forward and "svc/ewallet-edge-gateway" in forward
    private = next(c for c, _ in ex.cmds if "07-private-e2e" in c)
    assert "--evidence-class GKE_PRIVATE" in private and "--agents denied" in private and "--address 127.0.0.1" in private
    assert ex.cmds.index(next(x for x in ex.cmds if "07-private-e2e" in x[0])) < ex.cmds.index(
        next(x for x in ex.cmds if "public-open" in x[0] and "upgrade" in x[0]))


def test_l4_without_an_external_client_is_blocked_and_security_not_complete(tmp_path):
    state, _ = run_campaign(tmp_path)  # the client stays inside the approved CIDRs
    assert state["L4"] == "L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE"
    assert state["phases"]["L4-denied"] == "L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE"
    assert state["verdicts"]["G3B_SECURITY_STATUS"].startswith("INCOMPLETE")
    assert state["verdicts"]["G4_AUTOMATED_CHECKS_STATUS"] == "PARTIAL"


def test_l4_runs_once_this_client_is_outside_the_approved_cidrs(tmp_path):
    # inside for L5 (internet-gates), then the owner switches network: outside for L4
    state, ex = run_campaign(tmp_path, ips=("203.0.113.7", "198.51.100.200"), approve_grafana_root_url=True,
                             grafana_credentials=None)
    assert state["L4"].startswith("PASS") and state["L5"] == "PASS"
    l4 = next(c for c, _ in ex.cmds if "11-l4-denied" in c)
    assert "--agents denied" in l4 and "--evidence-class PUBLIC_INTERNET" in l4 and "--address 203.0.113.10" in l4


@pytest.mark.parametrize("passed", [True, False])
def test_an_external_jsonl_file_can_neither_pass_nor_fail_l4(tmp_path, passed):
    """Nothing proves a dropped-in file's source, age or campaign: it is ignored."""
    path, state = ready_state(tmp_path)
    ev = Path(state["config"]["campaign_dir"])
    (ev / "l4-external.jsonl").write_text(json.dumps({"row": "40", "check": "agents: refused from a non-allowed address",
                                                      "passed": passed, "evidence_class": "PUBLIC_INTERNET"}) + "\n")
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for))
    assert result["L4"] == "L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE"
    assert result["verdicts"]["G3B_SECURITY_STATUS"].startswith("INCOMPLETE")


def test_a_stale_l4_result_from_an_approved_network_cannot_pass(tmp_path):
    path, state = ready_state(tmp_path)
    ev = Path(state["config"]["campaign_dir"])
    (ev / "11-l4-denied.jsonl").write_text(json.dumps({"row": "40", "check": "agents: refused from a non-allowed address",
                                                       "passed": True, "evidence_class": "PUBLIC_INTERNET"}) + "\n")
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for))  # the client stays approved
    assert result["L4"] == "L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE"


def test_a_stale_result_is_removed_before_the_real_request(tmp_path):
    """Outside the CIDRs but the request writes nothing (e.g. it never reached the LB): the old
    passing file must not be read as this request's result."""
    path, state = ready_state(tmp_path)
    ev = Path(state["config"]["campaign_dir"])
    (ev / "11-l4-denied.jsonl").write_text(json.dumps({"row": "40", "check": "agents: refused from a non-allowed address",
                                                       "passed": True, "evidence_class": "PUBLIC_INTERNET"}) + "\n")
    silent = lambda text: [] if "--agents denied" in text else rows_for(text)
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", ips=("203.0.113.7", "198.51.100.200"), e2e_rows=silent))
    assert result["L4"] == "L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE"


@pytest.mark.parametrize("ips,rows,expected", [
    # back on the approved network when the request finished: the source is not proven
    (("203.0.113.7", "198.51.100.200", "203.0.113.7"), rows_for, "L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE"),
    # a result labelled with the wrong evidence class
    (("203.0.113.7", "198.51.100.200"),
     lambda text: [{**r, "evidence_class": "GKE_PRIVATE"} for r in rows_for(text)] if "--agents denied" in text else [],
     "L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE"),
])
def test_l4_needs_a_proven_source_and_a_public_internet_result(tmp_path, ips, rows, expected):
    path, _ = ready_state(tmp_path)
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", ips=ips, e2e_rows=rows))
    assert result["L4"] == expected and not result["verdicts"]["G3B_SECURITY_STATUS"] == "G3B_SECURITY_COMPLETE"


def test_l4_reachable_agents_from_a_non_approved_client_is_a_security_failure(tmp_path):
    path, _ = ready_state(tmp_path)
    reachable = lambda text: [{**r, "passed": False} for r in rows_for(text)] if "--agents denied" in text else rows_for(text)
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", ips=("203.0.113.7", "198.51.100.200"), e2e_rows=reachable))
    assert result["L4"] == "FAIL" and result["phases"]["L4-denied"].startswith("FAIL")
    assert result["verdicts"]["G3B_SECURITY_STATUS"] == "FAIL" and "staging-certificate" not in result["phases"]


def test_l5_fails_when_this_client_is_not_approved(tmp_path):
    state, ex = run_campaign(tmp_path, ips=("198.51.100.200",))
    assert state["L5"].startswith("FAIL") and state["phases"]["internet-gates"].startswith("FAIL")
    assert state["verdicts"]["G3B_SECURITY_STATUS"] == "FAIL"
    assert not has(ex, "10-e2e.jsonl")


def test_complete_security_and_g4_only_with_l4_and_grafana_verified(tmp_path):
    path, state = ready_state(tmp_path, approve_grafana_root_url=True)
    cfg = state["config"]
    grafana = tmp_path / "grafana-cred"
    grafana.write_text("admin:synthetic")
    state["config"]["grafana_credentials"] = str(grafana)
    camp.save(path, state)
    ex = Campaign(tmp_path / "sim.jsonl", ips=("203.0.113.7", "198.51.100.200"), e2e_rows=rows_for)
    result = camp.run(path, ex)
    v = result["verdicts"]
    assert v["G3B_SECURITY_STATUS"] == "G3B_SECURITY_COMPLETE" and v["G4_AUTOMATED_CHECKS_STATUS"] == "COMPLETE"
    assert result["GRAFANA"] == "PASS" and has(ex, "upgrade llm-agent")


@pytest.mark.parametrize("change,expected,upgrade", [
    ({}, "GRAFANA_NOT_APPROVED", False),
    ({"approve_grafana_root_url": True}, "GRAFANA_VERIFICATION_BLOCKED: no Grafana credentials", False),
])
def test_grafana_never_counts_as_verified_without_evidence(tmp_path, change, expected, upgrade):
    state, ex = run_campaign(tmp_path, **change)
    assert state["GRAFANA"] == expected and has(ex, "upgrade llm-agent") is upgrade
    assert state["phases"]["grafana"] == expected
    assert state["verdicts"]["G4_AUTOMATED_CHECKS_STATUS"] != "COMPLETE"


def test_grafana_release_diff_failure_blocks_the_upgrade(tmp_path):
    path, state = ready_state(tmp_path, approve_grafana_root_url=True)
    grafana = tmp_path / "grafana-cred"
    grafana.write_text("admin:synthetic")
    state["config"]["grafana_credentials"] = str(grafana)
    camp.save(path, state)
    ex = Campaign(tmp_path / "sim.jsonl", fail=("release_diff.py",), e2e_rows=rows_for)
    result = camp.run(path, ex)
    assert result["GRAFANA"].startswith("GRAFANA_VERIFICATION_BLOCKED: release_diff")
    assert not has(ex, "upgrade llm-agent") and result["verdicts"]["G4_AUTOMATED_CHECKS_STATUS"] != "COMPLETE"


def test_grafana_rows_failing_after_the_upgrade_is_a_failure(tmp_path):
    path, state = ready_state(tmp_path, approve_grafana_root_url=True)
    grafana = tmp_path / "grafana-cred"
    grafana.write_text("admin:synthetic")
    state["config"]["grafana_credentials"] = str(grafana)
    camp.save(path, state)
    bad = lambda text: [{**r, "passed": r["row"] != "39"} for r in GRAFANA_ROWS] if "--grafana-root-url" in text else rows_for(text)
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", e2e_rows=bad))
    assert result["GRAFANA"] == "GRAFANA_VERIFICATION_FAILED: rows 37-39"


def test_negative_tests_accepts_403_only_for_the_restricted_agents_probe():
    source = (EDGE / "negative_tests.py").read_text()
    assert 'refused = {401, 403} if host == "agents" else {401}' in source


# --- B. supervised mode, deadline, cancellation, locking ------------------------------------------

def test_supervised_mode_drops_only_the_unattended_gate(tmp_path):
    _, state = ready_state(tmp_path, mode="supervised", accept_local_only_risk=False, approve_live_campaign=True)
    assert "UNATTENDED_CLEANUP_READY" not in state["gates"]
    assert set(camp.COMMON_GATES) == set(state["gates"])
    assert state["gates"]["SAFE_CLEANUP_READY"] == "PASS" and state["gates"]["OWNER_LIVE_APPROVAL"] == "PASS"
    assert state["gates"]["L0_RENDER_READY"] == "PASS"


@pytest.mark.parametrize("change,gate", [
    ({"dns_credentials": None}, "SAFE_CLEANUP_READY"),
    ({"dns_offline_verified": False}, "SAFE_CLEANUP_READY"),
    ({"also_approve": []}, "PDB_APPROVAL_COMPLETE"),
    ({"approve_keda_pause": False}, "KEDA_SLEEP_APPROVAL_READY"),
    ({"approve_live_campaign": False}, "OWNER_LIVE_APPROVAL"),
])
def test_supervised_mode_still_refuses_missing_safety_inputs(tmp_path, change, gate, monkeypatch):
    if gate == "PDB_APPROVAL_COMPLETE":
        monkeypatch.setenv("EDGE_SIM_FAIL", "sleep-feasibility")
    args = {"mode": "supervised", "approve_live_campaign": True, **change}
    _, state = ready_state(tmp_path, **args)
    assert state["gates"][gate] == "FAIL"


def test_l0_render_gate_fails_offline_when_the_preflight_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGE_SIM_FAIL", "ownership_preflight.py render")
    _, state = ready_state(tmp_path, mode="supervised", approve_live_campaign=True)
    assert state["gates"]["L0_RENDER_READY"] == "FAIL"


def test_a_command_timeout_kills_its_whole_process_group():
    ex = camp.Executor()
    started = time.time()
    rc, _ = ex.run(["bash", "-c", "sleep 30 & echo $! > /dev/null; sleep 30"], timeout=1)
    assert rc == 124 and time.time() - started < 15
    leftover = subprocess.run(["pgrep", "-f", "sleep 30 & echo"], capture_output=True, text=True).stdout.strip()
    assert leftover == ""


def test_the_deadline_bounds_every_command(monkeypatch):
    monkeypatch.setattr(camp, "MIN_COMMAND_TIMEOUT", 1)
    ex = camp.Executor()
    ex.deadline = camp.now() + 1
    started = time.time()
    rc, _ = ex.run(["sleep", "20"], timeout=1800)
    assert rc == 124 and time.time() - started < 10


def test_terminate_current_stops_the_running_command():
    ex = camp.Executor()
    timer = threading.Timer(0.5, ex.terminate_current)
    timer.start()
    started = time.time()
    rc, _ = ex.run(["sleep", "20"])
    assert rc != 0 and time.time() - started < 10


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signals_stop_new_work_and_run_cleanup(tmp_path, sig):
    path, _ = ready_state(tmp_path)
    proc = cli("run", "--state", str(path), env={"EDGE_SIM_SLEEP": "0.2"})
    wait_for_phase(path, "L0-render")
    proc.send_signal(sig)
    proc.wait(timeout=90)
    state = json.loads(path.read_text())
    assert state["G3B_STATUS"] == f"stopped by {sig.name}" and state["CLEANUP_STATUS"] == "COMPLETE"
    assert "open-lb" not in state.get("phases", {})


def test_deadline_mid_campaign_starts_no_new_step(tmp_path, monkeypatch):
    path, _ = ready_state(tmp_path)
    ex = Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for)
    original = camp.now
    calls = {"n": 0}

    def clock():  # the deadline passes during the campaign, after the wake
        calls["n"] += 1
        return original() + (0 if calls["n"] < 40 else 4 * 3600)
    monkeypatch.setattr(camp, "now", clock)
    state = camp.run(path, ex)
    assert state["G3B_STATUS"] == "DEADLINE_EXPIRED" and state["CLEANUP_STATUS"] == "COMPLETE"
    assert "public-e2e" not in state["phases"]


def test_watchdog_never_cleans_up_beside_a_live_main_process_but_stops_it_after_the_grace(tmp_path):
    path, state = ready_state(tmp_path)
    main = subprocess.Popen(["sleep", "60"])
    try:
        state.update(campaign_active=True, main_pid=main.pid, heartbeat=camp.now(), deadline_epoch=camp.now() - 60)
        camp.save(path, state)
        with camp.Locked(path):  # the main process holds the campaign
            assert camp.watchdog(path, Recorder(tmp_path / "w.jsonl")) == "CLEANUP_ALREADY_RUNNING"
            state["deadline_epoch"] = camp.now() - camp.DEADLINE_GRACE_SECONDS - 60
            camp.save(path, state)
            assert camp.watchdog(path, Recorder(tmp_path / "w.jsonl")) == "SIGTERM_SENT_TO_MAIN"
        main.wait(timeout=10)
        assert main.returncode == -signal.SIGTERM
        assert json.loads(path.read_text()).get("CLEANUP_STATUS") is None  # the watchdog did not clean up itself
    finally:
        main.kill()


# --- C. ACME responder cleanup ---------------------------------------------------------------------

@pytest.mark.parametrize("failing", ["--environment staging", "--environment production"])
def test_failed_issuance_still_removes_the_responder_before_dns_and_lb(tmp_path, failing):
    state, ex = run_campaign(tmp_path, fail=(failing,))
    step = state["cleanup_steps"]["acme-responder-off"]
    assert step["ok"] and "edge-tls kept" in step["detail"] and state["ACME_STATUS"] == "responder-disabled"
    order = lambda needle: next(i for i, (c, _) in enumerate(ex.cmds) if needle(c))
    final_upgrade = order(lambda c: "upgrade ewallet-edge " in c and "g3b-values_final.yaml" in c)
    offline = order(lambda c: "dynu_dns.py" in c and " offline " in c)
    release = order(lambda c: "upgrade ewallet-edge-gateway" in c and "public-open" not in c)
    assert final_upgrade < offline < release
    assert state["CLEANUP_STATUS"] == "COMPLETE" and state["verdicts"]["G3B_DEPLOYMENT_STATUS"] == "FAIL"


def test_responder_already_disabled_needs_no_acme_cleanup(tmp_path):
    state, _ = run_campaign(tmp_path)
    assert state["phases"]["responder-off"] == "PASS" and state["ACME_STATUS"] == "responder-disabled"
    assert "acme-responder-off" not in state["cleanup_steps"]


def test_acme_cleanup_failure_is_reported_dns_lb_and_sleep_continue_and_retry_completes(tmp_path, monkeypatch):
    path, _ = ready_state(tmp_path)
    monkeypatch.setenv("EDGE_SIM_ACME_STAYS", "1")
    state = camp.run(path, Campaign(tmp_path / "sim.jsonl", fail=("--environment staging",), e2e_rows=rows_for))
    assert not state["cleanup_steps"]["acme-responder-off"]["ok"] and state["CLEANUP_STATUS"] == "CLEANUP_INCOMPLETE"
    assert state["cleanup_steps"]["lb-release"]["ok"] and state["cleanup_steps"]["sleep"]["ok"]
    monkeypatch.delenv("EDGE_SIM_ACME_STAYS")
    resumed = camp.cleanup(path, Campaign(tmp_path / "sim2.jsonl"), "retry")
    assert resumed["cleanup_steps"]["acme-responder-off"]["ok"] and resumed["CLEANUP_STATUS"] == "COMPLETE"


def test_an_in_flight_certbot_is_ended_before_the_responder_is_removed(tmp_path):
    path, state = ready_state(tmp_path)
    certbot = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        state.update(ACME_STATUS="issuing-production", acme_pgid=certbot.pid, wake_attempted=False)
        camp.save(path, state)
        result = camp.cleanup(path, Recorder(tmp_path / "c.jsonl"), "interrupted during ACME")
        certbot.wait(timeout=10)
        assert certbot.returncode == -signal.SIGTERM and result["cleanup_steps"]["acme-responder-off"]["ok"]
    finally:
        certbot.kill()


def test_cleanup_never_deletes_the_tls_secret_or_forces_anything(tmp_path):
    state, ex = run_campaign(tmp_path, fail=("--environment production",))
    cleanup_cmds = ex.cmds[next(i for i, (c, _) in enumerate(ex.cmds) if "g3b-values_final.yaml" in c):]
    joined = " ".join(c for c, _ in cleanup_cmds)
    assert " delete " not in joined and "--force" not in joined and "edge-tls" in joined  # only read
    assert not any("create secret tls edge-tls" in c for c, _ in cleanup_cmds)


def test_kill_during_acme_is_cleaned_up_by_the_watchdog(tmp_path):
    path, _ = ready_state(tmp_path)
    proc = cli("run", "--state", str(path), env={"EDGE_SIM_SLEEP": "0.3"})
    wait_for_phase(path, "staging-certificate")
    time.sleep(0.4)
    proc.kill()
    proc.wait(timeout=10)
    state = json.loads(path.read_text())
    state["heartbeat"] = time.time() - camp.STALE_SECONDS - 1
    camp.save(path, state)
    out = subprocess.run([sys.executable, str(EDGE / "g3b_campaign.py"), "--simulate", "watchdog", "--state", str(path)],
                         capture_output=True, text=True, timeout=180).stdout.strip()
    final = json.loads(path.read_text())
    assert out == "COMPLETE" and final["cleanup_steps"]["acme-responder-off"]["ok"] and final["cleanup_steps"]["sleep"]["ok"]


# --- ACME cleanup must keep every approved route and edge-tls (exact object identities) ------------

ROUTES = ["feature-api", "kagent-ui", "agent-registry", "grafana", "https-redirect"]
ACME = list(camp.ACME_OBJECTS)


class Listing(camp.Executor):
    """Answers the two reads acme_objects_gone makes."""

    def __init__(self, names, secret="secret/edge-tls", rc_secret=0):
        super().__init__()
        self.names, self.secret, self.rc_secret = names, secret, rc_secret

    def run(self, cmd, env=None, timeout=1800):
        if "secret" in cmd:
            return self.rc_secret, self.secret
        return 0, "\n".join(self.names)


def routes(*names):
    return [f"httproute.gateway.networking.k8s.io/{n}" for n in names]


@pytest.mark.parametrize("names,secret,ok,detail", [
    (routes(*ROUTES) + ["deployment.apps/ewallet-edge-gateway-x"], "secret/edge-tls", True, "edge-tls kept"),
    (routes("feature-api", "agent-registry", "grafana", "https-redirect"), "secret/edge-tls", False, "kagent-ui"),
    (routes("feature-api", "kagent-ui", "agent-registry", "https-redirect"), "secret/edge-tls", False, "grafana"),
    (routes("feature-api", "kagent-ui", "grafana", "https-redirect"), "secret/edge-tls", False, "agent-registry"),
    (routes("kagent-ui", "agent-registry", "grafana", "https-redirect"), "secret/edge-tls", False, "feature-api"),
    (routes("feature-api", "kagent-ui", "agent-registry", "grafana"), "secret/edge-tls", False, "https-redirect"),
    (routes(*ROUTES) + [ACME[0]], "secret/edge-tls", False, "acme-http01"),
    (routes(*ROUTES) + [ACME[1]], "secret/edge-tls", False, "deployment.apps/edge-acme-responder"),
    (routes(*ROUTES) + [ACME[2]], "secret/edge-tls", False, "service/edge-acme-responder"),
    (routes(*ROUTES) + [ACME[3]], "secret/edge-tls", False, "configmap/edge-acme-responder"),
    (routes(*ROUTES), "", False, "edge-tls missing"),
])
def test_acme_cleanup_verifies_every_route_each_acme_object_and_the_tls_secret(tmp_path, names, secret, ok, detail):
    _, state = ready_state(tmp_path)
    got, message = camp.acme_objects_gone(state, Listing(names, secret, 0 if secret else 1))
    assert got is ok and detail in message


def test_expected_routes_follow_the_approved_values(tmp_path):
    _, state = ready_state(tmp_path)
    assert camp.expected_routes(state) == set(ROUTES)
    final = Path(state["config"]["values_final"])
    values = json.loads(final.read_text())
    values["routes"] = {"kagent-ui": {"enabled": False}}  # an owner-disabled route is not required
    final.write_text(json.dumps(values))
    assert camp.expected_routes(state) == set(ROUTES) - {"kagent-ui"}
    ok, _ = camp.acme_objects_gone(state, Listing(routes("feature-api", "agent-registry", "grafana", "https-redirect")))
    assert ok


def test_a_name_that_merely_contains_acme_is_not_mistaken_for_the_responder(tmp_path):
    _, state = ready_state(tmp_path)
    ok, _ = camp.acme_objects_gone(state, Listing(routes(*ROUTES) + ["configmap/acme-unrelated-notes"]))
    assert ok


def test_a_failed_route_check_makes_cleanup_incomplete_and_a_replay_completes(tmp_path, monkeypatch):
    path, _ = ready_state(tmp_path)
    original = camp.acme_objects_gone
    monkeypatch.setattr(camp, "acme_objects_gone", lambda state, ex: (False, "application routes missing: ['grafana']"))
    state = camp.run(path, Campaign(tmp_path / "sim.jsonl", fail=("--environment staging",), e2e_rows=rows_for))
    assert state["CLEANUP_STATUS"] == "CLEANUP_INCOMPLETE" and "grafana" in state["cleanup_steps"]["acme-responder-off"]["detail"]
    monkeypatch.setattr(camp, "acme_objects_gone", original)
    resumed = camp.cleanup(path, Campaign(tmp_path / "sim2.jsonl"), "replay")
    assert resumed["CLEANUP_STATUS"] == "COMPLETE" and resumed["cleanup_steps"]["acme-responder-off"]["ok"]
