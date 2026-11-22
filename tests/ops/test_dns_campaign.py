"""Fail-closed DNS verification, the Dynu helper and the unattended G3-B campaign (offline).

No cloud, no DNS mutation: dig is faked, Dynu is a loopback HTTP server, and the campaign runs
with its Simulator (commands recorded, never executed). Addresses are documentation ranges."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
EDGE = ROOT / "scripts/edge"
sys.path.insert(0, str(EDGE))
LB, HOME_IP, DOMAIN = "203.0.113.10", "198.51.100.77", "edge.example.test"


def load(name):
    spec = importlib.util.spec_from_file_location(name, EDGE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dns, dynu, camp = load("dns_check"), load("dynu_dns"), load("g3b_campaign")


# --- dns_check: fail-closed ---------------------------------------------------------------------

def reply(status="NOERROR", records=(), rtype="A", aa=True, rc=0, raw=None):
    if raw is not None:
        return SimpleNamespace(returncode=rc, stdout=raw)
    flags = "qr aa rd" if aa else "qr rd ra"
    lines = [f";; ->>HEADER<<- opcode: QUERY, status: {status}, id: 1", f";; flags: {flags}; QUERY: 1, ANSWER: {len(records)}"]
    lines += [f"name. 120 IN {rtype} {r}" for r in records]
    return SimpleNamespace(returncode=rc, stdout="\n".join(lines) + "\n")


def fake(a=lambda server, name: reply(records=[LB]), aaaa=lambda server, name: reply(rtype="AAAA")):
    def run(cmd, capture_output, text):
        server, name, rtype = cmd[1][1:], cmd[2], cmd[3]
        if rtype == "NS":
            return reply(records=["ns1.dynu.example.", "ns3.dynu.example."], rtype="NS", aa=False)
        return a(server, name) if rtype == "A" else aaaa(server, name)
    return run


def verdict(mode, run, **kw):
    servers, err = dns.authoritative_servers(DOMAIN, "1.1.1.1", run)
    assert err is None
    expect = {"expect_ip": LB} if mode == "online" else {"expect_ip": None}
    return dns.check(DOMAIN, expect["expect_ip"], kw.get("offline_ip"), kw.get("released_ip"), servers, ["1.1.1.1", "8.8.8.8"], run)


def test_valid_online():
    assert verdict("online", fake())[0] == "DNS_ONLINE_OK"


@pytest.mark.parametrize("status", ["NXDOMAIN", "NOERROR"])  # NXDOMAIN and NOERROR/NODATA both prove "no record"
def test_valid_offline(status):
    run = fake(a=lambda s, n: reply(status=status), aaaa=lambda s, n: reply(status=status, rtype="AAAA"))
    assert verdict("offline", run, released_ip=LB)[0] == "DNS_OFFLINE_OK"


def test_valid_approved_offline_ip():
    run = fake(a=lambda s, n: reply(records=["192.0.2.1"]))
    assert verdict("offline", run, offline_ip="192.0.2.1", released_ip=LB)[0] == "DNS_OFFLINE_OK"


@pytest.mark.parametrize("mode,a,aaaa", [
    ("offline", lambda s, n: reply(records=[LB]), lambda s, n: reply(rtype="AAAA")),              # stale A (LB)
    ("offline", lambda s, n: reply(records=[HOME_IP]), lambda s, n: reply(rtype="AAAA")),         # home address
    ("online", lambda s, n: reply(records=[LB]), lambda s, n: reply(records=["2001:db8::1"], rtype="AAAA")),  # unexpected AAAA
    ("online", lambda s, n: reply(records=[LB] if s.startswith("ns") else [HOME_IP]), lambda s, n: reply(rtype="AAAA")),  # partial propagation
    ("online", lambda s, n: reply(records=[LB] if s == "ns1.dynu.example" else [HOME_IP]), lambda s, n: reply(rtype="AAAA")),  # mixed NS
])
def test_wrong_state_is_not_yet_and_withholds_values(mode, a, aaaa):
    got, rows = verdict(mode, fake(a=a, aaaa=aaaa))
    assert got.endswith("NOT_YET")
    assert HOME_IP not in json.dumps(rows) and "2001:db8::1" not in json.dumps(rows)


@pytest.mark.parametrize("bad", [
    reply(rc=9, raw=";; connection timed out; no servers could be reached\n"),     # timeout
    reply(status="SERVFAIL"),
    reply(status="REFUSED"),
    reply(rc=10, raw=""),                                                         # unreachable / network
    reply(raw="garbage without a header\n"),                                      # malformed
    reply(raw=""),                                                                # empty output, exit 0
    reply(records=["not-an-ip"]),                                                 # malformed record
])
@pytest.mark.parametrize("mode", ["online", "offline"])
def test_failures_are_check_failed_never_offline(bad, mode):
    run = fake(a=lambda s, n: bad if s == "8.8.8.8" else reply(status="NXDOMAIN"),
               aaaa=lambda s, n: reply(status="NXDOMAIN", rtype="AAAA"))
    assert verdict(mode, run)[0] == "DNS_CHECK_FAILED"


def test_a_non_authoritative_answer_from_an_authoritative_server_fails():
    run = fake(a=lambda s, n: reply(status="NXDOMAIN", aa=not s.startswith("ns")), aaaa=lambda s, n: reply(status="NXDOMAIN", rtype="AAAA"))
    assert verdict("offline", run)[0] == "DNS_CHECK_FAILED"


@pytest.mark.parametrize("ns_reply", [reply(rc=9, raw=""), reply(status="SERVFAIL"), reply(rtype="NS")])
def test_authoritative_lookup_failure_fails_the_cli(ns_reply, capsys):
    def run(cmd, capture_output, text):
        return ns_reply if cmd[3] == "NS" else reply()
    assert dns.main(["--domain", DOMAIN, "--expect-offline"], run=run) == 2
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["verdict"] == "DNS_CHECK_FAILED"


def test_cli_exit_codes(capsys):
    assert dns.main(["--domain", DOMAIN, "--expect-ip", LB], run=fake()) == 0
    assert dns.main(["--domain", DOMAIN, "--expect-offline"], run=fake()) == 1
    assert dns.main(["--domain", DOMAIN, "--expect-offline"], run=fake(a=lambda s, n: reply(status="SERVFAIL"))) == 2


# --- dynu_dns -----------------------------------------------------------------------------------

class Dynu(BaseHTTPRequestHandler):
    answer, seen = "good", []

    def do_GET(self):
        Dynu.seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
        body = Dynu.answer.encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Dynu)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    Dynu.seen, Dynu.answer = [], "good"
    yield f"http://127.0.0.1:{httpd.server_port}/nic/update"
    httpd.shutdown()


@pytest.fixture
def creds(tmp_path):
    path = tmp_path / "dynu.json"
    path.write_text(json.dumps({"hostname": DOMAIN, "username": "owner", "password_sha256": hashlib.sha256(b"synthetic-pw").hexdigest()}))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("action,expected", [
    (["online", "--ip", LB], {"hostname": DOMAIN, "myip": LB, "myipv6": "no"}),
    (["offline", "--mode", "no-ipv4"], {"hostname": DOMAIN, "myip": "no", "myipv6": "no"}),
    (["offline", "--mode", "signoff"], {"hostname": DOMAIN, "offline": "yes"}),
])
def test_dynu_requests_carry_credentials_only_in_the_header(server, creds, monkeypatch, action, expected):
    monkeypatch.setattr(dynu, "verify", lambda *a, **k: ("DNS_ONLINE_OK" if action[0] == "online" else "DNS_OFFLINE_OK", 1))
    assert dynu.main(["--credentials", str(creds), "--endpoint", server, "--execute", *action]) == 0
    from urllib.parse import parse_qsl, urlsplit
    request = Dynu.seen[0]
    assert dict(parse_qsl(urlsplit(request["path"]).query)) == expected
    user, secret = base64.b64decode(request["auth"].split()[1]).decode().split(":", 1)
    assert user == "owner" and secret == hashlib.sha256(b"synthetic-pw").hexdigest()
    assert secret not in request["path"] and "synthetic-pw" not in request["path"]


@pytest.mark.parametrize("answer", ["badauth", "nohost", "911", "abuse", "servererror"])
def test_dynu_error_answers_fail(server, creds, answer, capsys):
    Dynu.answer = answer
    assert dynu.main(["--credentials", str(creds), "--endpoint", server, "--execute", "offline", "--mode", "no-ipv4"]) == 2
    assert "DNS_UPDATE_FAILED" in capsys.readouterr().out


def test_dynu_update_without_a_proven_dns_state_fails(server, creds, monkeypatch):
    monkeypatch.setattr(dynu, "verify", lambda *a, **k: ("DNS_CHECK_FAILED", 15))
    assert dynu.main(["--credentials", str(creds), "--endpoint", server, "--execute", "offline", "--mode", "signoff"]) == 2


def test_dynu_dry_run_sends_nothing(server, creds):
    assert dynu.main(["--credentials", str(creds), "--endpoint", server, "online", "--ip", LB]) == 0
    assert Dynu.seen == []


def test_dynu_refuses_other_endpoints_and_unsafe_credential_files(creds, tmp_path):
    with pytest.raises(SystemExit, match="only to https://api.dynu.com"):
        dynu.send(dynu.load_credentials(str(creds)), {}, "https://evil.example/nic/update")
    loose = tmp_path / "loose.json"
    loose.write_text(creds.read_text())
    loose.chmod(0o644)
    with pytest.raises(SystemExit, match="chmod 600"):
        dynu.load_credentials(str(loose))
    with pytest.raises(SystemExit, match="inside the repository"):
        dynu.load_credentials(str(ROOT / "README.md"))
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"hostname": DOMAIN}))
    partial.chmod(0o600)
    with pytest.raises(SystemExit, match="lacks"):
        dynu.load_credentials(str(partial))


# --- g3b_campaign -------------------------------------------------------------------------------

def manifest(tmp_path):
    m = tmp_path / "manifest"
    m.mkdir()
    for name in ("workloads.json", "workload-requests.json", "disks-before.json"):
        (m / name).write_text("[]")
    (m / "buckets-before.txt").write_text("")
    (m / "MANIFEST.json").write_text(json.dumps({"cluster": "ewallet-dev"}))
    return m


def init_args(tmp_path, **kw):
    def secret(name, text):
        (tmp_path / name).write_text(text)
        return str(tmp_path / name)
    args = dict(campaign="g3b-test", campaign_dir=str(tmp_path / "campaign"), domain=DOMAIN, context="ctx", kubeconfig="kc",
                gateway_chart="chart.tgz", manifest=str(manifest(tmp_path)), approved_cidrs=secret("cidrs", "203.0.113.7/32\n"),
                edge_credentials=secret("edge", "edge:x"), grafana_credentials=None, dns_credentials=secret("dns", "{}"),
                dns_offline_mode="signoff", dns_offline_ip="192.0.2.207", dns_offline_ipv6="2001:db8:0:8888::207",
                approve_guarded_sleep=True, approve_keda_pause=True,
                approve_production_certificate=True, approve_grafana_root_url=False, dns_offline_verified=True,
                accept_local_only_risk=True, cloud_fallback_ready=False, also_approve=["ewallet-mcp/feature-rag-mcp"])
    args.update(kw)
    if "dns_transition_evidence" not in kw:  # a VERIFIED dns-transition run for exactly this configuration
        evidence = tmp_path / "transition-state.json"
        evidence.write_text(json.dumps({"scope": "dns-transition", "DNS_TRANSITION": "VERIFIED", "config": {
            "domain": args["domain"], "dns_offline_mode": args["dns_offline_mode"], "dns_offline_ip": args["dns_offline_ip"],
            "dns_offline_ipv6": args["dns_offline_ipv6"]}}))
        args["dns_transition_evidence"] = str(evidence)
    return SimpleNamespace(**args)


class Recorder(camp.Simulator):
    """The Simulator, plus scripted failures and a timeline of the cleanup-relevant commands."""

    def __init__(self, log, fail=(), plan_uncovered=("ewallet-mcp/feature-rag-mcp",)):
        super().__init__(log)
        self.fail, self.uncovered, self.cmds = fail, plan_uncovered, []

    def run(self, cmd, env=None, timeout=3600):
        text = " ".join(cmd)
        self.cmds.append((text, sorted((env or {}).keys())))
        if any(f in text for f in self.fail):
            return 1, ""
        if "plan-sleep" in text:
            return 0, json.dumps({"guarded_preparation": {"not_covered": list(self.uncovered)}})
        return super().run(cmd, env, timeout)

    def index(self, needle):
        return next(i for i, (c, _) in enumerate(self.cmds) if needle in c)


def ready_state(tmp_path, **kw):
    ex = Recorder(tmp_path / "sim.jsonl")
    state = camp.init(init_args(tmp_path, **kw), ex)
    return Path(state["config"]["campaign_dir"]) / "state.json", state


def test_init_records_gates_without_secrets(tmp_path):
    path, state = ready_state(tmp_path)
    assert state["gates"]["CLOUD_LAB_RECOVERY_MANIFEST_VALID"] == "PASS"
    assert state["gates"]["KEDA_SLEEP_APPROVAL_READY"] == "PASS" and state["gates"]["GUARDED_SLEEP_PLAN_VALID"] == "PASS"
    assert state["gates"]["CLIENT_ADDRESS_APPROVED"] == "PASS"
    assert "edge:x" not in path.read_text() and "203.0.113.7\"" not in json.dumps(state["gates"])
    assert oct(path.parent.stat().st_mode)[-3:] == "700"


@pytest.mark.parametrize("change,gate", [
    ({"approve_keda_pause": False}, "KEDA_SLEEP_APPROVAL_READY"),
    ({"approve_guarded_sleep": False}, "GUARDED_SLEEP_PLAN_VALID"),
    ({"dns_credentials": None}, "UNATTENDED_CLEANUP_READY"),
    ({"dns_offline_verified": False}, "UNATTENDED_CLEANUP_READY"),
    ({"accept_local_only_risk": False}, "UNATTENDED_CLEANUP_READY"),
    ({"edge_credentials": str(ROOT / "README.md")}, "UNATTENDED_CLEANUP_READY"),
])
def test_init_fails_a_gate_for_each_missing_precondition(tmp_path, change, gate):
    _, state = ready_state(tmp_path, **change)
    assert state["gates"][gate] == "FAIL"


def test_a_real_run_refuses_when_a_gate_failed(tmp_path):
    path, _ = ready_state(tmp_path, approve_keda_pause=False)

    class Explodes(camp.Executor):
        def run(self, *a, **k):
            raise AssertionError("no command may run")
    with pytest.raises(SystemExit, match="not waking"):
        camp.run(path, Explodes())


def test_full_simulated_campaign_cleans_dns_before_the_lb_and_sleeps_with_scoped_approvals(tmp_path):
    path, _ = ready_state(tmp_path)
    ex = Recorder(tmp_path / "sim.jsonl")
    state = camp.run(path, ex)
    assert state["G3B_STATUS"] == "PASS" and state["CLEANUP_STATUS"] == "COMPLETE" and state["LAST_VERIFIED_NODE_COUNT"] == 0
    online = next(i for i, (c, _) in enumerate(ex.cmds) if "dynu_dns.py" in c and " online " in c)
    offline = next(i for i, (c, _) in enumerate(ex.cmds) if "dynu_dns.py" in c and " offline " in c)
    release = next(i for i, (c, _) in enumerate(ex.cmds) if "upgrade ewallet-edge-gateway" in c and "public-open" not in c)
    sleep = next(i for i, (c, e) in enumerate(ex.cmds) if "cloud_lab.py sleep" in c)
    assert online < offline < release < sleep
    sleep_cmd, sleep_env = ex.cmds[sleep]
    assert "--also-approve ewallet-mcp/feature-rag-mcp" in sleep_cmd and "--scale-pdb-guarded --pause-keda" in sleep_cmd
    assert sleep_env == ["CLOUD_LAB_APPROVED", "CLOUD_LAB_GUARDED_SLEEP_APPROVAL", "CLOUD_LAB_KEDA_PAUSE_APPROVAL"]
    assert "--released-ip 203.0.113.10" in ex.cmds[offline][0]


def test_a_failed_phase_stops_new_work_and_still_cleans_up(tmp_path):
    path, _ = ready_state(tmp_path)
    ex = Recorder(tmp_path / "sim.jsonl", fail=("--environment staging",))
    state = camp.run(path, ex)
    assert state["G3B_STATUS"].startswith("FAIL at staging-certificate")
    assert not any("--environment production" in c for c, _ in ex.cmds)
    assert state["CLEANUP_STATUS"] == "COMPLETE" and state["cleanup_steps"]["lb-release"]["ok"]


def test_deadline_expiry_stops_and_cleans_up(tmp_path, monkeypatch):
    path, _ = ready_state(tmp_path)
    real_now = camp.now
    monkeypatch.setattr(camp, "DEADLINE_HOURS", -1)  # the deadline is already past right after wake
    state = camp.run(path, Recorder(tmp_path / "sim.jsonl"))
    assert state["G3B_STATUS"] == "DEADLINE_EXPIRED" and state["CLEANUP_STATUS"] == "COMPLETE"
    assert state["phases"] == {"wake": "PASS"}


def test_unsafe_dns_keeps_the_lb_sleeps_and_reports_incomplete_then_resumes(tmp_path):
    path, _ = ready_state(tmp_path)
    ex = Recorder(tmp_path / "sim.jsonl", fail=(" offline --mode",))
    state = camp.run(path, ex)
    assert state["CLEANUP_STATUS"] == "CLEANUP_INCOMPLETE"
    assert "LB KEPT" in state["cleanup_steps"]["lb-release"]["detail"]
    assert not any("upgrade ewallet-edge-gateway" in c and "public-open" not in c for c, _ in ex.cmds)
    assert state["cleanup_steps"]["sleep"]["ok"] and (Path(state["config"]["campaign_dir"]) / "ALERT.txt").exists()
    resumed = camp.cleanup(path, Recorder(tmp_path / "sim2.jsonl"), "resume")
    assert resumed["CLEANUP_STATUS"] == "COMPLETE" and resumed["cleanup_steps"]["lb-release"]["ok"]


def test_guarded_sleep_blocked_without_the_pdb_approval_and_no_bypass(tmp_path):
    path, _ = ready_state(tmp_path, also_approve=[])
    state = json.loads(path.read_text())
    state["gates"] = {g: "PASS" for g in camp.GATES}  # even if someone forced the gates
    camp.save(path, state)
    ex = Recorder(tmp_path / "sim.jsonl")
    result = camp.run(path, ex)
    assert "SLEEP_BLOCKED" in result["cleanup_steps"]["sleep"]["detail"] and result["CLEANUP_STATUS"] == "CLEANUP_INCOMPLETE"
    assert not any("cloud_lab.py sleep" in c for c, _ in ex.cmds)
    assert not any("container clusters resize" in c for c, _ in ex.cmds)


def test_invalid_state_is_refused(tmp_path):
    bad = tmp_path / "state.json"
    bad.write_text("{not json")
    with pytest.raises(SystemExit, match="invalid campaign state"):
        camp.load(bad)
    bad.write_text(json.dumps({"CAMPAIGN_ID": "x"}))
    with pytest.raises(SystemExit, match="missing"):
        camp.load(bad)


def test_watchdog_idle_ok_and_gives_up(tmp_path):
    path, state = ready_state(tmp_path)
    assert camp.watchdog(path, Recorder(tmp_path / "w.jsonl")) == "IDLE"
    state.update(campaign_active=True, heartbeat=camp.now(), main_pid=os.getpid(), deadline_epoch=camp.now() + 3600)
    camp.save(path, state)
    assert camp.watchdog(path, Recorder(tmp_path / "w.jsonl")) == "OK"
    state.update(deadline_epoch=camp.now() - 1, watchdog_attempts=3)
    camp.save(path, state)
    assert camp.watchdog(path, Recorder(tmp_path / "w.jsonl")).startswith("GAVE_UP")


def cli(*args, env=None):
    return subprocess.Popen([sys.executable, str(EDGE / "g3b_campaign.py"), "--simulate", *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env={**os.environ, **(env or {})})


def wait_for_phase(path, phase, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if json.loads(path.read_text()).get("CURRENT_PHASE") == phase:
                return
        except ValueError:
            pass
        time.sleep(0.1)
    raise AssertionError(f"phase {phase} not reached")


def test_sigterm_runs_cleanup(tmp_path):
    path, _ = ready_state(tmp_path)
    proc = cli("run", "--state", str(path), env={"EDGE_SIM_SLEEP": "0.2"})
    wait_for_phase(path, "edge-restricted")
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=60)
    state = json.loads(path.read_text())
    assert state["G3B_STATUS"] == "stopped by SIGTERM" and state["CLEANUP_STATUS"] == "COMPLETE"


def test_sudden_death_is_cleaned_up_by_the_watchdog(tmp_path):
    """kill -9: no trap or finally runs. The watchdog, a separate process, finds the stale
    heartbeat and the dead pid, takes the released lock and cleans up."""
    path, _ = ready_state(tmp_path)
    proc = cli("run", "--state", str(path), env={"EDGE_SIM_SLEEP": "0.2"})
    wait_for_phase(path, "dns-online")
    proc.kill()
    proc.wait(timeout=10)
    state = json.loads(path.read_text())
    assert state["campaign_active"] and state["CLEANUP_STATUS"] is None
    state["heartbeat"] = time.time() - camp.STALE_SECONDS - 1
    camp.save(path, state)
    done = subprocess.run([sys.executable, str(EDGE / "g3b_campaign.py"), "--simulate", "watchdog", "--state", str(path)],
                          capture_output=True, text=True, timeout=120)
    assert done.stdout.strip() == "COMPLETE", done.stderr
    final = json.loads(path.read_text())
    assert final["LAST_VERIFIED_NODE_COUNT"] == 0 and not final["campaign_active"]


def test_watchdog_does_not_interfere_with_a_live_main_process(tmp_path):
    path, _ = ready_state(tmp_path)
    proc = cli("run", "--state", str(path), env={"EDGE_SIM_SLEEP": "0.2"})
    wait_for_phase(path, "edge-restricted")
    out = subprocess.run([sys.executable, str(EDGE / "g3b_campaign.py"), "--simulate", "watchdog", "--state", str(path)],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=60)
    assert out == "OK"


def test_watchdog_units_render_and_verify(tmp_path):
    done = subprocess.run(["bash", str(EDGE / "install_watchdog.sh"), "--dest", str(tmp_path), "--state", str(tmp_path / "s.json")],
                          capture_output=True, text=True)
    assert done.returncode == 0
    service = (tmp_path / "edge-campaign-watchdog.service").read_text()
    assert f"{ROOT}/scripts/edge/g3b_campaign.py watchdog --state {tmp_path}/s.json" in service
    assert "@REPO@" not in service and "@STATE@" not in service and "@PATH@" not in service
    assert "systemd-inhibit" in service and "--simulate" not in service
    verify = subprocess.run(["systemd-analyze", "--user", "verify", str(tmp_path / "edge-campaign-watchdog.timer"),
                             str(tmp_path / "edge-campaign-watchdog.service")], capture_output=True, text=True)
    assert verify.returncode == 0, verify.stderr


@pytest.mark.parametrize("phase", ["wake", "open-lb", "dns-online"])
def test_a_kill_inside_a_mutating_phase_is_still_cleaned_up(tmp_path, phase):
    """The intent (wake, LB, DNS) is persisted before the command is sent, so a kill in the
    middle of that command still leaves cleanup knowing what to undo."""
    path, _ = ready_state(tmp_path)
    proc = cli("run", "--state", str(path), env={"EDGE_SIM_SLEEP": "1.0"})
    wait_for_phase(path, phase)
    time.sleep(0.5)  # inside the phase's first command
    proc.kill()
    proc.wait(timeout=10)
    state = json.loads(path.read_text())
    assert state["wake_attempted"]
    state["heartbeat"] = time.time() - camp.STALE_SECONDS - 1
    camp.save(path, state)
    done = subprocess.run([sys.executable, str(EDGE / "g3b_campaign.py"), "--simulate", "watchdog", "--state", str(path)],
                          capture_output=True, text=True, timeout=180)
    final = json.loads(path.read_text())
    assert done.stdout.strip() == "COMPLETE" and final["cleanup_steps"]["sleep"]["ok"]
    if phase in ("open-lb", "dns-online"):
        assert final["cleanup_steps"]["lb-release"]["ok"]
    if phase == "dns-online":
        assert final["cleanup_steps"]["dns-offline"]["ok"]
