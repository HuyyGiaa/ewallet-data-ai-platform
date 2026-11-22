"""One bounded Let's Encrypt production retry, only after a proven account-registration timeout
before any order (third Full G3-B attempt, 2026-11-22). Offline: Simulator, synthetic certbot logs."""

from __future__ import annotations

from pathlib import Path
import time

import pytest

from tests.ops.test_dns_campaign import Recorder, camp, ready_state

ACME = "https://acme-v02.api.letsencrypt.org"


def log_text(requests, error, received=False, at=None):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at or time.time())) + ",000"
    lines = [f"{stamp}:DEBUG:certbot._internal.main:certbot version: 5.10.0",
             f"{stamp}:DEBUG:acme.client:Sending GET request to {ACME}/directory."]
    for method, path in requests:
        lines.append(f"{stamp}:DEBUG:acme.client:Sending {method} request to {ACME}{path}:")
    if received:
        lines.append("Successfully received certificate.")
    if error:
        lines.append(f"{stamp}:ERROR:certbot._internal.log:An unexpected error occurred:")
        lines.append(f"{stamp}:ERROR:certbot._internal.log:{error}")
    return "\n".join(lines) + "\n"


TIMEOUT = "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='acme-v02.api.letsencrypt.org', port=443): Read timed out."
ACCOUNT = [("HEAD", "/acme/new-nonce"), ("POST", "/acme/new-acct")]
ORDER = ACCOUNT + [("POST", "/acme/new-order")]


class Certbot(Recorder):
    """Each production attempt writes its own certbot log (as certbot rotates logs per run)."""

    def __init__(self, log, private, attempts, live=False, on_attempt=None):
        super().__init__(log)
        self.private, self.attempts, self.live, self.on_attempt, self.production = Path(private), list(attempts), live, on_attempt, 0

    def run(self, cmd, env=None, timeout=3600):
        text = " ".join(cmd)
        if "issue_certificate.sh" in text and "--environment production" in text:
            self.cmds.append((text, []))
            rc, content = self.attempts[min(self.production, len(self.attempts) - 1)]
            self.production += 1
            logs = self.private / "tls/logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "letsencrypt.log").write_text(content)
            if self.live:
                (self.private / "tls/config/live/edge").mkdir(parents=True, exist_ok=True)
            if self.on_attempt:
                self.on_attempt(self)
            return rc, ""
        return super().run(cmd, env, timeout)


def campaign(tmp_path):
    path, state = ready_state(tmp_path, mode="supervised", approve_live_campaign=True)
    return path, state, state["config"]["private"]


def run(tmp_path, attempts, **kw):
    path, _, private = campaign(tmp_path)
    ex = Certbot(tmp_path / "sim.jsonl", private, attempts, **kw)
    return camp.run(path, ex), ex


def test_timeout_before_order_retries_once_and_success_passes(tmp_path):
    result, ex = run(tmp_path, [(1, log_text(ACCOUNT, TIMEOUT)), (0, log_text(ORDER, None, received=True))])
    assert ex.production == 2 and result["phases"]["production-certificate"] == "PASS"
    assert result["ACME_PRODUCTION_ATTEMPTS"] == 2 and result["ACME_PRODUCTION_FINAL_STATUS"] == "ISSUED"
    assert result["ACME_PRODUCTION_RETRY_REASON"].startswith("network timeout during ACME account registration")
    assert result["phases"]["responder-off"] == "PASS"


def test_retry_failing_too_stops_after_two_attempts_and_cleans_up(tmp_path):
    result, ex = run(tmp_path, [(1, log_text(ACCOUNT, TIMEOUT))] * 5)
    assert ex.production == 2 and result["ACME_PRODUCTION_ATTEMPTS"] == 2
    assert result["phases"]["production-certificate"].startswith("FAIL") and result["ACME_PRODUCTION_FINAL_STATUS"] == "FAILED"
    assert result["CLEANUP_STATUS"] == "COMPLETE" and result["SLEEP_STATUS"] == "VERIFIED"


@pytest.mark.parametrize("make,reason", [  # built at run time: the log must be written by this attempt
    (lambda: log_text(ACCOUNT, "acme.messages.Error: urn:ietf:params:acme:error:unauthorized"), "not a network timeout"),      # 4
    (lambda: log_text(ORDER + [("POST", "/acme/chall/abc")], "Some challenges have failed."), "a certificate order was sent"),  # 5
    (lambda: log_text(ACCOUNT, "acme.messages.Error: urn:ietf:params:acme:error:rateLimited :: too many registrations"),
     "not a network timeout"),                                                                                                  # 6
    (lambda: log_text(ORDER, TIMEOUT), "a certificate order was sent"),                                                        # 7
    (lambda: log_text([], TIMEOUT), "last ACME request was not account registration"),                                         # 7
    (lambda: log_text([("HEAD", "/acme/new-nonce")], TIMEOUT), "last ACME request was not account registration"),              # 7
    (lambda: log_text(ORDER, "kubectl: secret import failed", received=True), "a certificate was received"),                    # 8
    (lambda: log_text(ACCOUNT, TIMEOUT, at=time.time() - 7200), "predates this attempt"),                                      # 7 stale
    (lambda: "garbage without a timestamp\n" + TIMEOUT, "no start time"),                                                      # 7
], ids=["unauthorized", "http01-failed", "rate-limited", "timeout-after-order", "timeout-no-request", "timeout-at-nonce",
        "received-import-failed", "stale-log", "no-timestamp"])
def test_anything_but_a_proven_registration_timeout_is_not_retried(tmp_path, make, reason):
    result, ex = run(tmp_path, [(1, make())])
    assert ex.production == 1 and result["ACME_PRODUCTION_ATTEMPTS"] == 1
    assert reason in result["ACME_PRODUCTION_RETRY_REASON"] and result["ACME_PRODUCTION_RETRY_REASON"].startswith("no retry")
    assert result["phases"]["production-certificate"].startswith("FAIL") and result["CLEANUP_STATUS"] == "COMPLETE"


def test_certificate_on_disk_is_never_issued_again(tmp_path):                                                       # 8
    result, ex = run(tmp_path, [(1, log_text(ACCOUNT, TIMEOUT))], live=True)
    assert ex.production == 1 and "exists on disk" in result["ACME_PRODUCTION_RETRY_REASON"]


def test_missing_log_is_not_retried(tmp_path):
    path, state, private = campaign(tmp_path)
    state["state_path"] = str(path)

    class NoLog(Recorder):
        def run(self, cmd, env=None, timeout=3600):
            if "--environment production" in " ".join(cmd):
                return 1, ""
            return super().run(cmd, env, timeout)
    ex = NoLog(tmp_path / "s.jsonl")
    assert dict(camp.phases(state))["production-certificate"](state, ex)
    assert "no certbot log" in state["ACME_PRODUCTION_RETRY_REASON"]


def test_no_retry_when_the_deadline_is_close(tmp_path):                                                              # 9
    path, state, private = campaign(tmp_path)
    state["state_path"] = str(path)
    ex = Certbot(tmp_path / "s.jsonl", private, [(1, log_text(ACCOUNT, TIMEOUT))])
    ex.deadline = camp.now() + 600
    assert dict(camp.phases(state))["production-certificate"](state, ex)
    assert ex.production == 1 and "not enough time" in state["ACME_PRODUCTION_RETRY_REASON"]


def test_no_retry_after_a_signal_and_cleanup_still_runs(tmp_path):                                                    # 10
    def signal_arrives(ex):
        ex.stop_requested = True
    result, ex = run(tmp_path, [(1, log_text(ACCOUNT, TIMEOUT))], on_attempt=signal_arrives)
    assert ex.production == 1 and "stopped by a signal" in result["ACME_PRODUCTION_RETRY_REASON"]
    assert result["CLEANUP_STATUS"] == "COMPLETE"


def test_staging_is_never_retried(tmp_path):                                                                         # 11
    path, _, private = campaign(tmp_path)

    class StagingTimeout(Recorder):
        def run(self, cmd, env=None, timeout=3600):
            text = " ".join(cmd)
            if "--environment staging" in text:
                self.cmds.append((text, []))
                logs = Path(private) / "tls/logs"
                logs.mkdir(parents=True, exist_ok=True)
                (logs / "letsencrypt.log").write_text(log_text(ACCOUNT, TIMEOUT))
                return 1, ""
            return super().run(cmd, env, timeout)
    ex = StagingTimeout(tmp_path / "s.jsonl")
    result = camp.run(path, ex)
    assert sum("--environment staging" in c for c, _ in ex.cmds) == 1
    assert not any("--environment production" in c for c, _ in ex.cmds)
    assert result["phases"]["staging-certificate"].startswith("FAIL") and "ACME_PRODUCTION_ATTEMPTS" not in result


def test_no_second_certbot_while_the_first_process_group_is_alive(tmp_path, monkeypatch):                          # 12
    monkeypatch.setattr(camp, "pid_alive", lambda pid: pid == 424242)

    def first_started(ex):
        if ex.on_start:
            ex.on_start(424242)
    result, ex = run(tmp_path, [(1, log_text(ACCOUNT, TIMEOUT))], on_attempt=first_started)
    assert ex.production == 1 and "still alive" in result["ACME_PRODUCTION_RETRY_REASON"]


def test_attempts_are_sequential_with_one_fixed_delay(tmp_path, monkeypatch):
    path, state, private = campaign(tmp_path)
    state["state_path"] = str(path)
    sleeps, running = [], {"now": 0, "max": 0}

    class Real(Certbot):  # behaves like the real Executor for the delay (not a Simulator instance check)
        def run(self, cmd, env=None, timeout=3600):
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            try:
                return super().run(cmd, env, timeout)
            finally:
                running["now"] -= 1
    ex = Real(tmp_path / "s.jsonl", private, [(1, log_text(ACCOUNT, TIMEOUT)), (0, log_text(ORDER, None, received=True))])
    monkeypatch.setattr(camp, "Simulator", type("NotASimulator", (), {}))  # take the real 60 s delay path
    monkeypatch.setattr(camp.time, "sleep", lambda s: sleeps.append(s))
    assert dict(camp.phases(state))["production-certificate"](state, ex) is None
    assert ex.production == 2 and running["max"] == 1
    assert sum(sleeps) == camp.ACME_RETRY_DELAY_SECONDS and len(sleeps) == camp.ACME_RETRY_DELAY_SECONDS


def test_third_run_log_shape_is_classified_as_safe(tmp_path):
    """The exact request sequence of g3b-full-20261122T083830Z: directory, new-nonce, new-acct, ReadTimeout."""
    log = tmp_path / "letsencrypt.log"
    log.write_text(log_text(ACCOUNT, TIMEOUT))
    safe, reason = camp.acme_retry_safe(log, time.time() - 10, tmp_path / "live/edge")
    assert safe and "new-acct" in reason
