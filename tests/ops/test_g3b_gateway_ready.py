"""Gateway readiness after wake (first Full G3-B attempt, 2026-11-22: the gateway pod was rejected
while istiod's injection webhook was starting). Offline: Simulator commands, fake clock."""

from __future__ import annotations

import pytest

from tests.ops.test_dns_campaign import Recorder, camp, ready_state

GATEWAY = "deployment/ewallet-edge-gateway"


class Gateway(Recorder):
    """rollout status fails ``not_ready`` times (or always), and reports replicas as given."""

    def __init__(self, log, not_ready=0, desired="1", ready_after="1", fail=()):
        super().__init__(log, fail=fail)
        self.not_ready, self.desired, self.ready_after, self.rollouts = not_ready, desired, ready_after, 0

    def run(self, cmd, env=None, timeout=3600):
        text = " ".join(cmd)
        if f"rollout status {GATEWAY} --timeout=" in text and "--timeout=300s" not in text:
            self.cmds.append((text, []))
            self.rollouts += 1
            return (1, "") if self.not_ready is None or self.rollouts <= self.not_ready else (0, "")
        if "{.spec.replicas} {.status.readyReplicas}" in text:
            self.cmds.append((text, []))
            ready = "0" if self.not_ready is None or self.rollouts <= self.not_ready else self.ready_after
            return 0, f"{self.desired} {ready}".strip()
        return super().run(cmd, env, timeout)


class Clock:
    def __init__(self, monkeypatch):
        self.t, self.sleeps = 2_000_000.0, []
        monkeypatch.setattr(camp, "now", lambda: self.t)
        monkeypatch.setattr(camp.time, "sleep", self.sleep)
        monkeypatch.setenv("EDGE_SIM_CLOCK", "1")

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def g3b(tmp_path, **kw):
    return ready_state(tmp_path, mode="supervised", approve_live_campaign=True, **kw)


def names(state):
    return [n for n, _ in camp.phases(state)]


def lb_opened(ex):
    return any("upgrade ewallet-edge-gateway" in c and "public-open" in c for c, _ in ex.cmds)


def released_lb(ex):
    return any("upgrade ewallet-edge-gateway" in c and "public-open" not in c for c, _ in ex.cmds)


def test_runs_after_wake_and_baseline_and_before_any_edge_change(tmp_path):
    _, state = g3b(tmp_path)
    order = names(state)
    assert order.index("wake") < order.index("baseline") < order.index("gateway-ready") \
        < order.index("temporary-certificate") < order.index("edge-restricted")
    (tmp_path / "t").mkdir()
    _, transition = ready_state(tmp_path / "t", scope="dns-transition", mode="supervised", approve_dns_transition_test=True,
                                dns_offline_verified=False)
    assert "gateway-ready" in names(transition)  # the DNS-transition scope gets the same gate


def test_backend_checks_are_kept_and_the_gate_is_read_only(tmp_path):
    path, _ = g3b(tmp_path)
    ex = Gateway(tmp_path / "s.jsonl")
    result = camp.run(path, ex)
    assert result["phases"]["baseline"] == "PASS" and ex.index("platform_checks.py") < ex.index(f"rollout status {GATEWAY}")
    assert any("backends --snapshot-out" in c for c, _ in ex.cmds)
    before_edge = [c for c, _ in ex.cmds[:ex.index("issue_certificate.sh")] if "ewallet-edge-gateway" in c]
    assert before_edge and all(" get deployment " in c or " rollout status " in c for c in before_edge)  # no restart, scale or delete
    assert result["GATEWAY_READY"].startswith("READY (1/1")


def test_ready_after_an_initial_delay(tmp_path, monkeypatch):
    path, _ = g3b(tmp_path)
    Clock(monkeypatch)
    ex = Gateway(tmp_path / "s.jsonl", not_ready=3)
    result = camp.run(path, ex)
    assert result["phases"]["gateway-ready"] == "PASS" and ex.rollouts == 4
    assert result["GATEWAY_READY"] == "READY (1/1, rollout status attempt 4)"
    assert result["phases"]["edge-restricted"] == "PASS" and lb_opened(ex)


def test_never_ready_fails_within_the_budget_opens_no_lb_and_cleans_up(tmp_path, monkeypatch):
    path, _ = g3b(tmp_path)
    clock = Clock(monkeypatch)
    ex = Gateway(tmp_path / "s.jsonl", not_ready=None)
    start = clock.t
    result = camp.run(path, ex)
    assert result["phases"]["gateway-ready"] == "FAIL: gateway Deployment not Ready within 25 min (no edge change made)"
    assert "edge-restricted" not in result["phases"] and "temporary-certificate" not in result["phases"]
    assert not lb_opened(ex) and not any("dynu_dns.py" in c for c, _ in ex.cmds)
    assert result["CLEANUP_STATUS"] == "COMPLETE" and result["SLEEP_STATUS"] == "VERIFIED"
    waited = clock.t - start
    assert waited <= camp.GATEWAY_READY_SECONDS + 60  # one bounded budget, not retries x timeouts
    timeouts = [int(c.split("--timeout=")[1].rstrip("s")) for c, _ in ex.cmds if f"rollout status {GATEWAY} --timeout=" in c]
    assert timeouts[0] <= camp.GATEWAY_READY_SECONDS and timeouts == sorted(timeouts, reverse=True)


def test_budget_is_capped_by_the_campaign_deadline(tmp_path, monkeypatch):
    path, state = g3b(tmp_path)
    state["state_path"] = str(path)
    clock = Clock(monkeypatch)
    ex = Gateway(tmp_path / "s.jsonl", not_ready=None)
    ex.deadline = clock.t + 10 * 60  # only 10 min left
    error = dict(camp.phases(state))["gateway-ready"](state, ex)
    assert error == "gateway Deployment not Ready within 10 min (no edge change made)"
    first = [c for c, _ in ex.cmds if f"rollout status {GATEWAY} --timeout=" in c][0]
    assert int(first.split("--timeout=")[1].rstrip("s")) <= 600
    ex.deadline = clock.t - 1
    assert dict(camp.phases(state))["gateway-ready"](state, ex) == "DEADLINE_EXPIRED"


@pytest.mark.parametrize("desired", ["0", "", "x"])
def test_zero_or_unknown_desired_replicas_never_pass(tmp_path, desired):
    path, _ = g3b(tmp_path)
    ex = Gateway(tmp_path / "s.jsonl", desired=desired)
    result = camp.run(path, ex)
    assert result["phases"]["gateway-ready"].startswith("FAIL: gateway Deployment has no desired replica")
    assert ex.rollouts == 0 and not lb_opened(ex) and result["CLEANUP_STATUS"] == "COMPLETE"


def test_rollout_complete_but_no_ready_replica_does_not_pass(tmp_path):
    path, _ = g3b(tmp_path)
    ex = Gateway(tmp_path / "s.jsonl", ready_after="0")
    result = camp.run(path, ex)
    assert result["phases"]["gateway-ready"].startswith("FAIL") and not lb_opened(ex)


def test_simulation_without_a_clock_is_bounded_to_three_attempts(tmp_path):
    path, _ = g3b(tmp_path)
    ex = Gateway(tmp_path / "s.jsonl", not_ready=None)
    result = camp.run(path, ex)
    assert ex.rollouts == 3 and result["phases"]["gateway-ready"].startswith("FAIL")


def test_cleanup_after_a_gateway_failure_releases_nothing_it_did_not_open(tmp_path, monkeypatch):
    path, _ = g3b(tmp_path)
    Clock(monkeypatch)
    ex = Gateway(tmp_path / "s.jsonl", not_ready=None)
    result = camp.run(path, ex)
    assert result["PUBLIC_LB_CREATED"] is False and result["DNS_TARGET_STATE"] == "untouched"
    assert not released_lb(ex) and "dns-verify-before-release" not in result["cleanup_steps"]
    assert result["verdicts"]["G3B_DEPLOYMENT_STATUS"] == "FAIL"
