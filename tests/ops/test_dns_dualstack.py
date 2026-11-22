"""Dual-stack Dynu offline verification (G3-B, 2026-11-22 owner observations).

Dynu's Offline mode answers its offline server on both A and AAAA; myip=no answered nochg and
left the old A record. Offline: dig is faked, Dynu is a loopback server, the campaign is
simulated. The offline addresses below are documentation stand-ins, not Dynu's."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ops.test_dns_campaign import (  # noqa: F401  (creds and server are fixtures)
    DOMAIN, Dynu, Recorder, camp, creds, dns, dynu, fake, ready_state, reply, server)

OFF4, OFF6 = "192.0.2.207", "2001:db8:0:8888::207"
LB, HOME = "203.0.113.10", "198.51.100.77"


def check(a, aaaa, released=LB, v4=OFF4, v6=OFF6, run=None):
    run = run or fake(a=lambda s, n: a(s, n), aaaa=lambda s, n: aaaa(s, n))
    servers, err = dns.authoritative_servers(DOMAIN, "1.1.1.1", run)
    assert err is None
    return dns.check(DOMAIN, None, v4, released, servers, ["1.1.1.1", "8.8.8.8"], run, v6)


A = lambda *ips: (lambda s, n: reply(records=list(ips)))
AAAA = lambda *ips: (lambda s, n: reply(records=list(ips), rtype="AAAA"))
NONE_A = lambda s, n: reply(status="NOERROR")
NONE_AAAA = lambda s, n: reply(status="NOERROR", rtype="AAAA")


@pytest.mark.parametrize("a,aaaa", [
    (A(OFF4), AAAA(OFF6)),        # Dynu offline server on both families
    (A(OFF4), NONE_AAAA),         # approved IPv4, no AAAA
    (NONE_A, AAAA(OFF6)),         # no A, approved IPv6
    (NONE_A, NONE_AAAA),          # nothing at all
    (lambda s, n: reply(status="NXDOMAIN"), lambda s, n: reply(status="NXDOMAIN", rtype="AAAA")),
])
def test_offline_accepts_only_approved_addresses_or_proven_absence(a, aaaa):
    assert check(a, aaaa)[0] == "DNS_OFFLINE_OK"


@pytest.mark.parametrize("a,aaaa", [
    (A(HOME), AAAA(OFF6)),                                    # unexpected IPv4 (the registration address)
    (A(OFF4), AAAA("2001:db8::bad")),                         # unexpected IPv6
    (A(LB), AAAA(OFF6)),                                      # the released LB address
    (A(OFF4), AAAA(OFF6, "2001:db8::bad")),                   # an extra AAAA
    (lambda s, n: reply(records=[OFF4] if s.startswith("ns") else [HOME]), AAAA(OFF6)),  # partial propagation
])
def test_offline_anything_else_is_not_yet_and_withheld(a, aaaa):
    verdict, rows = check(a, aaaa)
    assert verdict == "DNS_OFFLINE_NOT_YET"
    assert HOME not in json.dumps(rows) and "2001:db8::bad" not in json.dumps(rows)


def test_offline_ipv6_without_an_approved_offline_ipv6_is_not_yet():
    """The owner's first run: --offline-ip only, while Dynu also answered AAAA."""
    verdict, rows = check(A(OFF4), AAAA(OFF6), v6=None)
    assert verdict == "DNS_OFFLINE_NOT_YET" and all(r["AAAA_ok"] is False and r["A_ok"] for r in rows)


@pytest.mark.parametrize("bad", [reply(rc=9, raw=""), reply(status="SERVFAIL", rtype="AAAA"), reply(status="REFUSED", rtype="AAAA"),
                                 reply(raw="no header\n")])
@pytest.mark.parametrize("where", ["authoritative", "resolver"])
def test_aaaa_failures_are_check_failed(bad, where):
    hit = (lambda s: s.startswith("ns")) if where == "authoritative" else (lambda s: s == "8.8.8.8")
    verdict, _ = check(A(OFF4), lambda s, n: bad if hit(s) else reply(records=[OFF6], rtype="AAAA"))
    assert verdict == "DNS_CHECK_FAILED"


def test_authoritative_ns_timeout_fails_the_cli(capsys):
    run = lambda cmd, capture_output, text: reply(rc=9, raw="") if cmd[3] == "NS" else reply()
    assert dns.main(["--domain", DOMAIN, "--expect-offline", "--offline-ip", OFF4, "--offline-ipv6", OFF6], run=run) == 2


def test_cli_normalises_ipv6_and_rejects_wrong_families(capsys):
    run = fake(a=A(OFF4), aaaa=AAAA(OFF6))
    assert dns.main(["--domain", DOMAIN, "--expect-offline", "--offline-ip", OFF4,
                     "--offline-ipv6", "2001:0db8:0000:8888:0000:0000:0000:0207"], run=run) == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["verdict"] == "DNS_OFFLINE_OK" and summary["A_status"] == summary["AAAA_status"] == "OK"
    for args in (["--offline-ipv6", OFF4], ["--offline-ip", OFF6], ["--offline-ipv6", "zzz"], ["--expect-ip", OFF6]):
        mode = [] if args[0] == "--expect-ip" else ["--expect-offline"]
        with pytest.raises(SystemExit):
            dns.main(["--domain", DOMAIN, *mode, *args], run=run)
    with pytest.raises(SystemExit, match="apply to --expect-offline only"):
        dns.main(["--domain", DOMAIN, "--expect-ip", LB, "--offline-ipv6", OFF6], run=run)


@pytest.mark.parametrize("aaaa", [AAAA("2001:db8::bad"), AAAA(OFF6)])
def test_online_still_refuses_any_aaaa(aaaa):
    run = fake(a=A(LB), aaaa=aaaa)
    servers, _ = dns.authoritative_servers(DOMAIN, "1.1.1.1", run)
    verdict, _ = dns.check(DOMAIN, LB, None, None, servers, ["1.1.1.1"], run)
    assert verdict == "DNS_ONLINE_NOT_YET"


def test_summary_line_reports_where_and_how_long(capsys):
    run = fake(a=lambda s, n: reply(status="SERVFAIL") if s == "8.8.8.8" else reply(records=[OFF4]), aaaa=AAAA(OFF6))
    assert dns.main(["--domain", DOMAIN, "--expect-offline", "--offline-ip", OFF4, "--offline-ipv6", OFF6], run=run) == 2
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["verdict"] == "DNS_CHECK_FAILED" and summary["failed_servers"] == ["8.8.8.8"]
    assert summary["check_errors"] == 6 and "duration_s" in summary


# --- dynu_dns -------------------------------------------------------------------------------

def test_signoff_sends_only_offline_yes_and_forwards_both_offline_addresses(server, creds, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(dynu, "verify", lambda domain, expect, *a, **k: (seen.setdefault("expect", expect), ("DNS_OFFLINE_OK", 1))[1])
    assert dynu.main(["--credentials", str(creds), "--endpoint", server, "--execute", "offline", "--mode", "signoff",
                      "--offline-ip", OFF4, "--offline-ipv6", OFF6, "--released-ip", LB]) == 0
    from urllib.parse import parse_qsl, urlsplit
    assert dict(parse_qsl(urlsplit(Dynu.seen[0]["path"]).query)) == {"hostname": DOMAIN, "offline": "yes"}
    assert seen["expect"] == ["--expect-offline", "--offline-ip", OFF4, "--offline-ipv6", OFF6, "--released-ip", LB]
    out = capsys.readouterr().out
    assert "Basic " not in out and "password" not in out and json.loads(out.splitlines()[0])["update_status"].startswith("accepted")


@pytest.mark.parametrize("mode,answer", [("no-ipv4", "nochg"), ("signoff", "nochg"), ("signoff", "good")])
def test_an_accepted_update_is_not_offline_until_dns_says_so(server, creds, monkeypatch, mode, answer):
    """The owner's case: myip=no -> nochg, and the old A record stayed."""
    Dynu.answer = answer
    monkeypatch.setattr(dynu, "verify", lambda *a, **k: ("DNS_OFFLINE_NOT_YET", 10))
    assert dynu.main(["--credentials", str(creds), "--endpoint", server, "--execute", "offline", "--mode", mode]) == 1
    monkeypatch.setattr(dynu, "verify", lambda *a, **k: ("DNS_CHECK_FAILED", 10))
    assert dynu.main(["--credentials", str(creds), "--endpoint", server, "--execute", "offline", "--mode", mode]) == 2


def test_auth_failure_stops_before_verification(server, creds, monkeypatch):
    Dynu.answer = "badauth"
    monkeypatch.setattr(dynu, "verify", lambda *a, **k: pytest.fail("no verification after a rejected update"))
    assert dynu.main(["--credentials", str(creds), "--endpoint", server, "--execute", "offline", "--mode", "signoff"]) == 2


def test_verification_progress_lines_and_bounded_attempts(capsys):
    calls = []

    def run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        return type("Done", (), {"returncode": 1, "stdout": json.dumps({"verdict": "DNS_OFFLINE_NOT_YET", "A_status": "OK",
                                                                          "AAAA_status": "NOT_OK", "check_errors": 0, "duration_s": 2.0})})()
    verdict, attempts = dynu.verify(DOMAIN, ["--expect-offline"], 3, 0, run=run, sleep=lambda _: None)
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert verdict == "DNS_OFFLINE_NOT_YET" and attempts == 3 and len(calls) == 3
    assert [l["attempt"] for l in lines] == [1, 2, 3] and lines[0]["AAAA_status"] == "NOT_OK"


def test_ctrl_c_during_verification_sends_no_further_update(server, creds, monkeypatch, capsys):
    def interrupted(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(dynu, "verify", interrupted)
    assert dynu.main(["--credentials", str(creds), "--endpoint", server, "--execute", "offline", "--mode", "signoff"]) == 130
    assert len(Dynu.seen) == 1 and "INTERRUPTED" in capsys.readouterr().out


def test_dry_run_sends_nothing_and_wrong_families_are_refused(server, creds):
    assert dynu.main(["--credentials", str(creds), "--endpoint", server, "offline", "--mode", "signoff",
                      "--offline-ip", OFF4, "--offline-ipv6", OFF6]) == 0
    assert Dynu.seen == []
    for args in (["--offline-ip", OFF6], ["--offline-ipv6", OFF4]):
        with pytest.raises(SystemExit, match="is not an IPv"):
            dynu.main(["--credentials", str(creds), "--endpoint", server, "offline", "--mode", "signoff", *args])


# --- campaign -------------------------------------------------------------------------------

def test_campaign_forwards_both_offline_addresses_and_defaults_to_signoff(tmp_path):
    path, state = ready_state(tmp_path, dns_offline_mode="signoff", dns_offline_ip=OFF4, dns_offline_ipv6=OFF6)
    assert state["config"]["dns_offline_ipv6"] == OFF6
    ex = Recorder(tmp_path / "sim.jsonl")
    result = camp.run(path, ex)
    offline = next(c for c, _ in ex.cmds if "dynu_dns.py" in c and " offline " in c)
    assert f"--mode signoff --offline-ip {OFF4} --offline-ipv6 {OFF6} --released-ip 203.0.113.10" in offline
    assert "--attempts 10 --interval 30" in offline and result["cleanup_steps"]["lb-release"]["ok"]
    assert camp.main.__code__  # CLI default checked below
    import inspect
    assert 'default="signoff"' in inspect.getsource(camp.main)


@pytest.mark.parametrize("reason", ["unexpected AAAA", "unexpected A", "DNS timeout", "API signoff failure"])
def test_any_unproven_dns_state_keeps_the_lb_and_a_later_cleanup_releases_it(tmp_path, reason):
    path, _ = ready_state(tmp_path, dns_offline_mode="signoff", dns_offline_ip=OFF4, dns_offline_ipv6=OFF6)
    state = camp.run(path, Recorder(tmp_path / "sim.jsonl", fail=(" offline --mode signoff",)))
    assert state["CLEANUP_STATUS"] == "CLEANUP_INCOMPLETE" and "LB KEPT" in state["cleanup_steps"]["lb-release"]["detail"]
    assert state["cleanup_steps"]["sleep"]["ok"]  # guarded sleep still ran, under its own gates
    resumed = camp.cleanup(path, Recorder(tmp_path / "sim2.jsonl"), f"after {reason} was fixed")
    assert resumed["CLEANUP_STATUS"] == "COMPLETE" and resumed["cleanup_steps"]["lb-release"]["ok"]


@pytest.mark.parametrize("v4,v6", [(OFF6, OFF6), (OFF4, OFF4), ("not-an-ip", None)])
def test_init_refuses_wrong_offline_addresses(tmp_path, v4, v6):
    _, state = ready_state(tmp_path, dns_offline_ip=v4, dns_offline_ipv6=v6)
    assert state["gates"]["SAFE_CLEANUP_READY"] == "FAIL"
