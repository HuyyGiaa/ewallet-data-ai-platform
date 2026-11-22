"""Full G3-B pre-live patch: owner evidence window, return to the approved network, IPv4 client
lookup and the certbot preflight gate (offline: Simulator commands, fake clock)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import threading

import pytest

from tests.ops.test_dns_campaign import Recorder, camp, ready_state
from tests.ops.test_g3b_supervised import Campaign, rows_for

INSIDE, OUTSIDE = "203.0.113.7", "198.51.100.200"


def g3b(tmp_path, window=45, **kw):
    args = dict(mode="supervised", approve_live_campaign=True, evidence_window_minutes=window)
    args.update(kw)
    return ready_state(tmp_path, **args)


class Clock:
    """now() and time.sleep() on a fake clock; ``on_sleep`` runs at each window poll."""

    def __init__(self, monkeypatch, on_sleep=None):
        self.t, self.on_sleep, self.sleeps = 1_000_000.0, on_sleep, []
        monkeypatch.setattr(camp, "now", lambda: self.t)
        monkeypatch.setattr(camp.time, "sleep", self.sleep)
        monkeypatch.setenv("EDGE_SIM_WINDOW_WAIT", "1")

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if seconds and self.on_sleep:
            self.on_sleep(self)
        self.t += seconds


def names(state):
    return [n for n, _ in camp.phases(state)]


# --- configuration --------------------------------------------------------------------------------

def test_disabled_by_default_cleanup_follows_public_e2e(tmp_path):
    path, state = g3b(tmp_path, window=0)
    assert "evidence-window" not in names(state)
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for))
    assert "evidence-window" not in result["phases"] and result["CLEANUP_STATUS"] == "COMPLETE"
    assert camp.window_status(result)["EVIDENCE_WINDOW"] == "NOT_CONFIGURED"


@pytest.mark.parametrize("change,problem", [
    ({"mode": "unattended", "accept_local_only_risk": True}, "needs --scope g3b --mode supervised"),
    ({"scope": "dns-transition", "approve_dns_transition_test": True, "dns_offline_verified": False}, "needs --scope g3b --mode supervised"),
    ({"window": 61}, "must be 0..60"),
    ({"window": -1}, "must be 0..60"),
])
def test_window_only_for_supervised_g3b_and_at_most_60_minutes(tmp_path, change, problem):
    change = dict(change)
    _, state = g3b(tmp_path, window=change.pop("window", 45), **change)
    assert any(problem in p for p in state["gate_problems"])
    assert not all(v == "PASS" for v in state["gates"].values())


def test_window_is_the_last_phase_after_public_e2e(tmp_path):
    _, state = g3b(tmp_path)
    assert names(state)[-2:] == ["public-e2e", "evidence-window"]


# --- the window -----------------------------------------------------------------------------------

def test_simulation_closes_the_window_without_waiting(tmp_path):
    path, _ = g3b(tmp_path)
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for))
    assert result["phases"]["evidence-window"] == "closed: simulated (no wait)"
    assert result["CLEANUP_STATUS"] == "COMPLETE" and result["SLEEP_STATUS"] == "VERIFIED"


def test_window_expires_then_cleanup_runs_and_nothing_runs_during_it(tmp_path, monkeypatch):
    path, _ = g3b(tmp_path, window=2)
    ex = Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for)
    during = []
    Clock(monkeypatch, on_sleep=lambda c: during.append(len(ex.cmds)))
    result = camp.run(path, ex)
    assert result["phases"]["evidence-window"] == "closed: window expired"
    assert len(set(during)) == 1  # no command at all while the window was open
    signoff = max(i for i, (c, _) in enumerate(ex.cmds) if "offline --mode signoff" in c)
    assert result["CLEANUP_STATUS"] == "COMPLETE" and signoff >= during[0]  # cleanup after the window


def test_continue_during_the_window_closes_it(tmp_path, monkeypatch):
    path, _ = g3b(tmp_path, window=45)
    answers = []

    def owner(clock):
        if not answers:
            answers.append(camp.request_continue(path))
    clock = Clock(monkeypatch, on_sleep=owner)
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for))
    assert answers[0][0] == 0 and answers[0][1].startswith("CONTINUE_ACCEPTED")
    assert result["phases"]["evidence-window"] == "closed: owner continue"
    assert sum(clock.sleeps) < 45 * 60 and result["CLEANUP_STATUS"] == "COMPLETE"


def test_continue_is_rejected_before_and_after_the_window(tmp_path):
    path, _ = g3b(tmp_path)
    rc, message = camp.request_continue(path)
    assert rc == 1 and message.startswith("CONTINUE_REJECTED")
    camp.run(path, Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for))
    rc, message = camp.request_continue(path)
    assert rc == 1 and message.startswith("CONTINUE_REJECTED")
    assert not (Path(json.loads(path.read_text())["config"]["campaign_dir"]) / camp.CONTINUE_MARKER).exists()


def test_a_stale_marker_from_before_the_window_does_not_close_it(tmp_path, monkeypatch):
    path, state = g3b(tmp_path, window=1)
    (Path(state["config"]["campaign_dir"]) / camp.CONTINUE_MARKER).write_text("stale")
    Clock(monkeypatch)
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for))
    assert result["phases"]["evidence-window"] == "closed: window expired"


def test_concurrent_continue_requests_create_one_marker_and_never_touch_state(tmp_path, monkeypatch):
    path, _ = g3b(tmp_path)
    seen = {}

    def owners(clock):
        if seen:
            return
        before = path.read_bytes()
        results, barrier = [], threading.Barrier(8)

        def one():
            barrier.wait()
            results.append(camp.request_continue(path))
        threads = [threading.Thread(target=one) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seen.update(results=results, unchanged=path.read_bytes() == before)
    Clock(monkeypatch, on_sleep=owners)
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for))
    messages = [m for _, m in seen["results"]]
    assert sum(m.startswith("CONTINUE_ACCEPTED") for m in messages) == 1
    assert sum(m == "CONTINUE_ALREADY_REQUESTED" for m in messages) == 7
    assert seen["unchanged"] and json.loads(path.read_text())["CLEANUP_STATUS"] == "COMPLETE"
    assert result["phases"]["evidence-window"] == "closed: owner continue"


def window_function(state):
    return dict(camp.phases(state))["evidence-window"]


def test_window_ends_30_minutes_before_the_deadline(tmp_path, monkeypatch):
    path, state = g3b(tmp_path, window=60)
    state["state_path"] = str(path)
    clock = Clock(monkeypatch)
    ex = Recorder(tmp_path / "sim.jsonl")
    ex.deadline = clock.t + 40 * 60  # 40 min left: only 10 min of window allowed
    window_function(state)(state, ex)
    assert state["EVIDENCE_WINDOW"] == "closed: deadline cleanup reserve"
    assert abs(sum(clock.sleeps) - 10 * 60) < 1


def test_no_window_inside_the_cleanup_reserve(tmp_path, monkeypatch):
    path, state = g3b(tmp_path, window=45)
    state["state_path"] = str(path)
    clock = Clock(monkeypatch)
    ex = Recorder(tmp_path / "sim.jsonl")
    ex.deadline = clock.t + 20 * 60
    window_function(state)(state, ex)
    assert state["EVIDENCE_WINDOW"].startswith("skipped") and not clock.sleeps


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_during_the_window_still_runs_cleanup(tmp_path, monkeypatch, signum):
    path, _ = g3b(tmp_path, window=45)
    sent = []

    def owner(clock):
        if not sent:
            sent.append(True)
            os.kill(os.getpid(), signum)
    Clock(monkeypatch, on_sleep=owner)
    try:
        result = camp.run(path, Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for))
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)
    assert result["EVIDENCE_WINDOW"] == "closed: signal"
    assert result["G3B_STATUS"].startswith("stopped by")
    assert result["CLEANUP_STATUS"] == "COMPLETE" and result["SLEEP_STATUS"] == "VERIFIED"


def test_a_failed_public_e2e_never_opens_the_window(tmp_path, monkeypatch):
    path, _ = g3b(tmp_path)
    Clock(monkeypatch)
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", fail=("14-e2e.jsonl",), e2e_rows=rows_for))
    assert result["phases"]["public-e2e"].startswith("FAIL") and "evidence-window" not in result["phases"]
    assert result["CLEANUP_STATUS"] == "COMPLETE"


def test_window_does_not_change_verdicts(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with_window, _ = g3b(tmp_path / "a")
    without, _ = g3b(tmp_path / "b", window=0)
    a = camp.run(with_window, Campaign(tmp_path / "a.jsonl", e2e_rows=rows_for))["verdicts"]
    b = camp.run(without, Campaign(tmp_path / "b.jsonl", e2e_rows=rows_for))["verdicts"]
    assert a == b


def test_production_certificate_gate_still_stops_before_the_window(tmp_path):
    path, _ = g3b(tmp_path, approve_production_certificate=False)
    result = camp.run(path, Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for))
    assert result["phases"]["production-certificate"] == "FAIL: production certificate not approved"
    assert "evidence-window" not in result["phases"] and result["cleanup_steps"]["dns-verify-before-release"]["ok"]


def test_status_shows_window_state_and_remaining_minutes(tmp_path, monkeypatch):
    path, _ = g3b(tmp_path, window=45)
    seen = {}

    def peek(clock):
        if not seen:
            seen.update(camp.window_status(json.loads(path.read_text())))
            camp.request_continue(path)
    Clock(monkeypatch, on_sleep=peek)
    camp.run(path, Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for))
    assert seen["EVIDENCE_WINDOW"] == "open" and 44 < seen["EVIDENCE_WINDOW_REMAINING_MIN"] <= 45


# --- return to the approved network -----------------------------------------------------------------

def test_return_to_the_approved_network_after_l4(tmp_path):
    path, _ = g3b(tmp_path, window=0)
    ex = Campaign(tmp_path / "sim.jsonl", ips=(INSIDE, OUTSIDE, OUTSIDE, OUTSIDE, INSIDE), e2e_rows=rows_for)
    result = camp.run(path, ex)
    assert result["L4"].startswith("PASS") and result["phases"]["approved-network"] == "PASS"
    assert result["CLIENT_NETWORK"] == "approved" and result["phases"]["public-e2e"] == "PASS"


@pytest.mark.parametrize("last,message", [(OUTSIDE, "not back inside the approved CIDRs"), ("", "could not be verified")])
def test_client_not_back_stops_before_certificates_and_cleans_up(tmp_path, last, message):
    path, _ = g3b(tmp_path, window=0)
    ex = Campaign(tmp_path / "sim.jsonl", ips=(INSIDE, OUTSIDE, OUTSIDE, last), e2e_rows=rows_for)
    result = camp.run(path, ex)
    assert result["L4"].startswith("PASS")
    assert result["phases"]["approved-network"].startswith("FAIL") and message in result["phases"]["approved-network"]
    assert not any("issue_certificate.sh" in c and "staging" in c for c, _ in ex.cmds)
    assert result["CLEANUP_STATUS"] == "COMPLETE"


def test_client_address_lookups_use_ipv4(tmp_path):
    path, _ = g3b(tmp_path, window=0)
    ex = Campaign(tmp_path / "sim.jsonl", e2e_rows=rows_for)
    camp.run(path, ex)
    lookups = [c for c, _ in ex.cmds if "myip.opendns.com" in c]
    assert lookups and all(c.startswith("dig -4 +short myip.opendns.com @resolver1.opendns.com") for c in lookups)
    assert camp.MYIP_LOOKUP[:2] == ["dig", "-4"]
    assert INSIDE not in path.read_text()  # the address itself is never stored


# --- certbot preflight ------------------------------------------------------------------------------

class Certbot(Recorder):
    def __init__(self, log, rc=0, out="certbot 5.10.0"):
        super().__init__(log)
        self.rc, self.out = rc, out

    def run(self, cmd, env=None, timeout=3600):
        if "certbot --version" in " ".join(cmd):
            self.cmds.append((" ".join(cmd), []))
            return self.rc, self.out
        return super().run(cmd, env, timeout)


@pytest.mark.parametrize("rc,out,gate", [(0, "certbot 5.10.0", "PASS"), (127, "", "FAIL"), (0, "certbot 4.0.0", "FAIL")])
def test_certbot_gate(tmp_path, rc, out, gate):
    args = camp.argparse.Namespace(**{**vars(__import__("tests.ops.test_dns_campaign", fromlist=["init_args"]).init_args(tmp_path)),
                                     "mode": "supervised", "approve_live_campaign": True})
    ex = Certbot(tmp_path / "sim.jsonl", rc, out)
    state = camp.init(args, ex)
    assert state["gates"]["CERTBOT_READY"] == gate
    assert any(c.endswith(f"--from {camp.CERTBOT_PIN} certbot --version") for c, _ in ex.cmds)


def test_dns_transition_scope_has_no_certbot_gate(tmp_path):
    _, state = ready_state(tmp_path, scope="dns-transition", mode="supervised", approve_dns_transition_test=True,
                           dns_offline_verified=False)
    assert "CERTBOT_READY" not in state["gates"] and set(state["gates"]) == set(camp.TRANSITION_GATES)
