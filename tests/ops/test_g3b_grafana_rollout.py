"""Grafana rollout readiness after the root URL upgrade (sixth Full G3-B run, 2026-11-23: public-e2e
ran while the Grafana pod was being replaced and rows 37-39 got 503). Offline: Simulator, fake clock."""

from __future__ import annotations

import pytest

from tests.ops.test_dns_campaign import camp, ready_state
from tests.ops.test_g3b_supervised import Campaign, rows_for

ROLLOUT = "rollout status deployment/grafana"
COUNTS = "get deployment grafana -o jsonpath={.spec.replicas} {.status.readyReplicas}"


class Grafana(Campaign):
    """rollout status answers ``rollout_rc`` (after ``delay`` fake seconds); the Grafana counts answer ``counts``."""

    def __init__(self, log, rollout_rc=0, counts="1 1", delay=0, clock=None, **kw):
        super().__init__(log, e2e_rows=rows_for, **kw)
        self.rollout_rc, self.counts, self.delay, self.clock = rollout_rc, counts, delay, clock

    def run(self, cmd, env=None, timeout=1800):
        text = " ".join(cmd)
        if ROLLOUT in text:
            self.cmds.append((text, [timeout]))
            if self.clock and self.delay:
                self.clock[0] += min(self.delay, timeout)
            return self.rollout_rc, ""
        if COUNTS in text:
            self.cmds.append((text, []))
            return 0, self.counts
        return super().run(cmd, env, timeout)


def campaign(tmp_path):
    path, state = ready_state(tmp_path, approve_grafana_root_url=True)
    grafana = tmp_path / "grafana-cred"
    grafana.write_text("admin:synthetic")
    state["config"]["grafana_credentials"] = str(grafana)
    camp.save(path, state)
    return path, state


def index(ex, needle):
    return next((i for i, (c, _) in enumerate(ex.cmds) if needle in c), None)


def public_e2e_ran(ex):
    return index(ex, "14-e2e.jsonl") is not None


def test_a_normal_rollout_is_checked_before_public_e2e(tmp_path):
    path, _ = campaign(tmp_path)
    ex = Grafana(tmp_path / "s.jsonl")
    result = camp.run(path, ex)
    upgrade, rollout, counts, e2e = index(ex, "upgrade llm-agent"), index(ex, ROLLOUT), index(ex, COUNTS), index(ex, "14-e2e.jsonl")
    assert upgrade < rollout < counts < e2e
    assert f"-n ewallet-agents {ROLLOUT} --timeout=300s" in ex.cmds[rollout][0]
    assert result["phases"]["grafana"] == "PASS" and result["GRAFANA"] == "PASS"   # rows 37-39 then pass
    assert result["GRAFANA_READY"] == "rollout complete, 1 ready"


def test_b_a_delayed_rollout_is_waited_for_and_public_e2e_comes_after(tmp_path, monkeypatch):
    path, _ = campaign(tmp_path)
    clock = [1_000_000.0]
    monkeypatch.setattr(camp, "now", lambda: clock[0])
    ex = Grafana(tmp_path / "s.jsonl", delay=55, clock=clock)
    result = camp.run(path, ex)
    assert index(ex, ROLLOUT) < index(ex, "14-e2e.jsonl") and result["GRAFANA"] == "PASS"


@pytest.mark.parametrize("rollout_rc,counts", [(1, "1 0"), (0, "1 0"), (0, "1"), (0, "")],
                         ids=["rollout-timeout", "complete-but-0-ready", "no-ready-field", "unreadable"])
def test_c_grafana_not_ready_fails_the_phase_and_skips_public_e2e(tmp_path, rollout_rc, counts):
    path, _ = campaign(tmp_path)
    ex = Grafana(tmp_path / "s.jsonl", rollout_rc=rollout_rc, counts=counts)
    result = camp.run(path, ex)
    assert result["phases"]["grafana"].startswith("FAIL: Grafana rollout not Ready within 300 s")
    assert result["GRAFANA"].startswith("GRAFANA_VERIFICATION_FAILED: Grafana not Ready")
    assert not public_e2e_ran(ex) and "public-e2e" not in result["phases"]
    assert result["CLEANUP_STATUS"] == "COMPLETE" and result["SLEEP_STATUS"] == "VERIFIED"


def test_d_a_failed_upgrade_does_not_wait_and_keeps_the_existing_behaviour(tmp_path):
    path, _ = campaign(tmp_path)
    ex = Grafana(tmp_path / "s.jsonl", fail=("upgrade llm-agent",))
    result = camp.run(path, ex)
    assert index(ex, ROLLOUT) is None and result["GRAFANA"] == "GRAFANA_VERIFICATION_FAILED: upgrade exit 1"
    assert result["phases"]["grafana"] == "GRAFANA_VERIFICATION_FAILED: upgrade exit 1"   # recorded as observed, as before
    assert result["CLEANUP_STATUS"] == "COMPLETE"


def test_release_diff_checks_and_upgrade_scope_are_unchanged(tmp_path):
    path, _ = campaign(tmp_path)
    ex = Grafana(tmp_path / "s.jsonl")
    camp.run(path, ex)
    diffs = [c for c, _ in ex.cmds if "release_diff.py" in c]
    assert len(diffs) == 2 and "--allow-grafana-root-url https://observe." in diffs[1]
    upgrades = [c for c, _ in ex.cmds if "upgrade llm-agent" in c]
    assert len(upgrades) == 1 and "--set grafanaExternal.rootUrl=https://observe." in upgrades[0]
    grafana_cmds = [c for c, _ in ex.cmds if "grafana" in c and "kubectl" in c]
    assert grafana_cmds and all(" rollout status " in c or " get deployment " in c for c in grafana_cmds)  # read-only


def grafana_phase(state):
    return dict(camp.phases(state))["grafana"]


def test_e_the_budget_never_exceeds_the_campaign_deadline(tmp_path, monkeypatch):
    path, state = campaign(tmp_path)
    state["state_path"] = str(path)
    clock = [2_000_000.0]
    monkeypatch.setattr(camp, "now", lambda: clock[0])
    ex = Grafana(tmp_path / "s.jsonl")
    ex.deadline = clock[0] + 120                               # 2 min left: the wait is capped at 120 s
    assert grafana_phase(state)(state, ex) is None
    rollout = ex.cmds[index(ex, ROLLOUT)]
    assert "--timeout=120s" in rollout[0] and rollout[1][0] <= 150


def test_e_no_wait_after_the_deadline_or_a_signal(tmp_path, monkeypatch):
    path, state = campaign(tmp_path)
    state["state_path"] = str(path)
    clock = [3_000_000.0]
    monkeypatch.setattr(camp, "now", lambda: clock[0])
    ex = Grafana(tmp_path / "s.jsonl")
    ex.deadline = clock[0] - 1
    assert grafana_phase(state)(state, ex) == "Grafana rollout not verified (deadline or signal)"
    assert index(ex, ROLLOUT) is None
    ex2 = Grafana(tmp_path / "s2.jsonl")
    ex2.stop_requested = True
    assert grafana_phase(state)(state, ex2) == "Grafana rollout not verified (deadline or signal)"
    assert index(ex2, ROLLOUT) is None and state["GRAFANA"].startswith("GRAFANA_VERIFICATION_FAILED")


def test_e_a_real_sigterm_during_the_rollout_wait_stops_before_public_e2e_and_cleans_up(tmp_path):
    import os
    import signal
    path, _ = campaign(tmp_path)

    class SignalAtRollout(Grafana):
        def run(self, cmd, env=None, timeout=1800):
            if ROLLOUT in " ".join(cmd):
                os.kill(os.getpid(), signal.SIGTERM)       # run()'s handler records it; the phase then ends
            return super().run(cmd, env, timeout)
    ex = SignalAtRollout(tmp_path / "s.jsonl")
    try:
        result = camp.run(path, ex)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)
    assert result["G3B_STATUS"] == "stopped by SIGTERM" and result["phases"]["grafana"].startswith("FAIL: stopped by SIGTERM")
    assert not public_e2e_ran(ex) and "public-e2e" not in result["phases"]
    assert result["CLEANUP_STATUS"] == "COMPLETE" and result["SLEEP_STATUS"] == "VERIFIED"


def test_namespace_comes_from_the_release_values(tmp_path, monkeypatch):
    path, state = campaign(tmp_path)
    state["state_path"] = str(path)

    class OtherNamespace(Grafana):
        def run(self, cmd, env=None, timeout=1800):
            text = " ".join(cmd)
            if "get values llm-agent" in text:
                self.cmds.append((text, []))
                return 0, "namespace:\n  name: observability-test\n"
            return super().run(cmd, env, timeout)
    ex = OtherNamespace(tmp_path / "s.jsonl")
    assert grafana_phase(state)(state, ex) is None
    assert f"-n observability-test {ROLLOUT}" in ex.cmds[index(ex, ROLLOUT)][0]
