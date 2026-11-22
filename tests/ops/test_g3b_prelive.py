"""G3-B pre-live: edge Basic Auth Secret synchronisation and the first Dynu DNS transition (offline).

The Basic Auth script runs for real against a fake kubectl (no cluster). The campaign runs with
its Simulator (commands recorded, never executed). Addresses are documentation ranges."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.ops.test_dns_campaign import EDGE, LB, Recorder, camp, ready_state

SCRIPT = EDGE / "create_basic_auth.sh"
PASSWORD = "Synthetic-Pr3live-Password-0123456789abcdef"
OFF4, OFF6 = "192.0.2.207", "2001:db8:0:8888::207"

FAKE_KUBECTL = r"""#!/usr/bin/env bash
# fake kubectl: logs its arguments, serves $FAKE_DIR/secret.b64, records what is applied
printf '%s\n' "$*" >> "$FAKE_DIR/argv.log"
case " $* " in
  *" get secret edge-basic-auth "*)
    [ -f "$FAKE_DIR/fail" ] && exit 1
    cat "$FAKE_DIR/secret.b64" 2>/dev/null; exit 0 ;;
  *" create secret generic "*)
    for a in "$@"; do case "$a" in --from-file=htpasswd=*) f=${a#--from-file=htpasswd=} ;; esac; done
    printf 'apiVersion: v1\nkind: Secret\ndata:\n  htpasswd: %s\n' "$(base64 -w0 < "$f")"; exit 0 ;;
  *" apply -f - "*)
    sed -n 's/^  htpasswd: //p' > "$FAKE_DIR/secret.b64"; echo "secret/edge-basic-auth configured"; exit 0 ;;
esac
exit 3
"""


def sha_line(user, password):
    return f"{user}:{{SHA}}{base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()}"


@pytest.fixture
def kube(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kubectl").write_text(FAKE_KUBECTL)
    (bin_dir / "kubectl").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_DIR": str(tmp_path)}
    return tmp_path, env


def cred_file(tmp_path, text=f"edgeadmin:{PASSWORD}\n", mode=0o600):
    f = tmp_path / "edge-cred"
    f.write_text(text)
    f.chmod(mode)
    return f


def secret(tmp_path, *lines):
    (tmp_path / "secret.b64").write_text(base64.b64encode(("\n".join(lines) + "\n").encode()).decode())


def auth(env, cred, mode="--check"):
    return subprocess.run(["bash", str(SCRIPT), "--credentials-file", str(cred), mode, "--kubeconfig", "kc", "--context", "ctx"],
                          capture_output=True, text=True, env=env)


# --- create_basic_auth.sh -----------------------------------------------------------------------

def test_check_matches_the_owner_credentials_and_prints_nothing_secret(kube):
    tmp, env = kube
    secret(tmp, sha_line("edgeadmin", PASSWORD))
    done = auth(env, cred_file(tmp))
    assert done.returncode == 0 and done.stdout.strip() == "EDGE_AUTH_MATCH"
    hashed = sha_line("edgeadmin", PASSWORD).split("}")[1]
    for text in (done.stdout, done.stderr, (tmp / "argv.log").read_text()):
        assert PASSWORD not in text and hashed not in text


@pytest.mark.parametrize("lines,word", [
    ((sha_line("edgeadmin", "an-old-password-from-g3a1-xxxxxxxx"),), "EDGE_AUTH_MISMATCH"),
    ((sha_line("edge", PASSWORD),), "EDGE_AUTH_MISMATCH"),
    ((sha_line("edgeadmin", PASSWORD), sha_line("other", "another-password-xxxxxxxxxxxx")), "EDGE_AUTH_MISMATCH"),
    (None, "EDGE_AUTH_MISSING"),
])
def test_check_reports_mismatch_and_missing(kube, lines, word):
    tmp, env = kube
    if lines:
        secret(tmp, *lines)
    done = auth(env, cred_file(tmp))
    assert done.returncode == 1 and done.stdout.strip() == word


def test_check_fails_closed_when_the_secret_cannot_be_read(kube):
    tmp, env = kube
    (tmp / "fail").write_text("")
    done = auth(env, cred_file(tmp))
    assert done.returncode == 2 and done.stdout.startswith("EDGE_AUTH_CHECK_FAILED")


@pytest.mark.parametrize("text,mode,reason", [
    (f"edgeadmin:{PASSWORD}\n", 0o644, "mode 600"),
    ("edgeadmin:short\n", 0o600, "at least 24"),
    (f"edgeadmin:{PASSWORD}\nsecond:{PASSWORD}\n", 0o600, "exactly one"),
    (f"{PASSWORD}\n", 0o600, "not user:password"),
    (f"bad user:{PASSWORD}\n", 0o600, "invalid username"),
])
def test_unsafe_or_malformed_credential_files_are_refused_before_kubectl(kube, text, mode, reason):
    tmp, env = kube
    done = auth(env, cred_file(tmp, text, mode))
    assert done.returncode == 2 and reason in done.stdout
    assert not (tmp / "argv.log").exists()


def test_apply_writes_the_same_htpasswd_line_and_check_then_matches(kube):
    tmp, env = kube
    secret(tmp, sha_line("edge", "the-g3a1-password-xxxxxxxxxxxxxx"))
    cred = cred_file(tmp)
    assert auth(env, cred).returncode == 1
    done = auth(env, cred, "--apply")
    assert done.returncode == 0 and done.stdout.strip() == "EDGE_AUTH_APPLIED"
    stored = base64.b64decode((tmp / "secret.b64").read_text()).decode().strip()
    assert stored == sha_line("edgeadmin", PASSWORD)
    assert auth(env, cred).stdout.strip() == "EDGE_AUTH_MATCH"
    argv = (tmp / "argv.log").read_text()
    assert PASSWORD not in argv and stored.split("}")[1] not in argv and PASSWORD not in done.stdout + done.stderr


def test_stdin_mode_is_unchanged_for_the_local_validators(kube):
    tmp, env = kube
    done = subprocess.run(["bash", str(SCRIPT), "edge", "ewallet-ingress", "ctx"], input=PASSWORD, capture_output=True,
                          text=True, env=env)
    assert done.returncode == 0
    assert base64.b64decode((tmp / "secret.b64").read_text()).decode().strip() == sha_line("edge", PASSWORD)


# --- campaign: edge-auth ------------------------------------------------------------------------

def run_campaign(tmp_path, monkeypatch, auth_mode=None, fail=(), **kw):
    if auth_mode:
        monkeypatch.setenv("EDGE_SIM_EDGE_AUTH", auth_mode)
    path, state = ready_state(tmp_path, mode="supervised", approve_live_campaign=True, **kw)
    ex = Recorder(tmp_path / "sim.jsonl", fail=fail)
    return camp.run(path, ex), ex, state


def lb_index(ex):
    return next(i for i, (c, _) in enumerate(ex.cmds) if "upgrade ewallet-edge-gateway" in c and "public-open" in c)


def lb_opened(ex):
    return any("upgrade ewallet-edge-gateway" in c and "public-open" in c for c, _ in ex.cmds)


@pytest.mark.parametrize("auth_mode,approved,gate", [
    (None, False, "PASS"), ("mismatch", False, "FAIL"), ("mismatch", True, "PASS"), ("error", True, "FAIL")])
def test_init_edge_auth_gate(tmp_path, monkeypatch, auth_mode, approved, gate):
    if auth_mode:
        monkeypatch.setenv("EDGE_SIM_EDGE_AUTH", auth_mode)
    _, state = ready_state(tmp_path, mode="supervised", approve_live_campaign=True, approve_edge_secret_sync=approved)
    assert state["gates"]["EDGE_AUTH_READY"] == gate
    assert state["edge_auth_at_init"] == {None: "EDGE_AUTH_MATCH", "mismatch": "EDGE_AUTH_MISMATCH",
                                          "error": "EDGE_AUTH_CHECK_FAILED"}[auth_mode]


def test_matching_secret_needs_no_mutation(tmp_path, monkeypatch):
    state, ex, _ = run_campaign(tmp_path, monkeypatch)
    assert state["EDGE_AUTH"] == "EDGE_AUTH_MATCH" and state["phases"]["edge-auth"] == "PASS"
    assert not any("--apply" in c or "rollout restart" in c for c, _ in ex.cmds)


def test_mismatch_without_approval_stops_before_private_e2e_and_the_lb(tmp_path, monkeypatch):
    state, ex, _ = run_campaign(tmp_path, monkeypatch, "mismatch")
    assert state["phases"]["edge-auth"].startswith("FAIL: EDGE_AUTH_MISMATCH")
    assert "private-e2e" not in state["phases"] and not lb_opened(ex)
    assert not any("--apply" in c for c, _ in ex.cmds)
    assert state["CLEANUP_STATUS"] == "COMPLETE" and state["SLEEP_STATUS"] == "VERIFIED"


def test_approved_sync_applies_restarts_rechecks_then_private_e2e_then_lb(tmp_path, monkeypatch):
    state, ex, _ = run_campaign(tmp_path, monkeypatch, "mismatch", approve_edge_secret_sync=True)
    assert state["EDGE_AUTH"] == "EDGE_AUTH_SYNCED"
    order = [ex.index("create_basic_auth.sh --credentials-file"), ex.index(" --apply --kubeconfig"),
             ex.index("rollout restart deployment/ewallet-edge-gateway"),
             ex.index("rollout status deployment/ewallet-edge-gateway"),
             ex.index("07-private-e2e.jsonl"), ex.index("07-private-negative.jsonl"), lb_index(ex)]
    assert order == sorted(order)
    recheck = [i for i, (c, _) in enumerate(ex.cmds) if "create_basic_auth.sh" in c and "--check" in c]
    assert any(ex.index("rollout status") < i < ex.index("07-private-e2e.jsonl") for i in recheck)


def test_secret_that_does_not_take_blocks_the_lb(tmp_path, monkeypatch):
    state, ex, _ = run_campaign(tmp_path, monkeypatch, "stuck", approve_edge_secret_sync=True)
    assert state["EDGE_AUTH"].startswith("EDGE_AUTH_SYNC_FAILED") and not lb_opened(ex)


@pytest.mark.parametrize("failing", ["rollout status", "07-private-e2e.jsonl", "07-private-negative.jsonl"])
def test_gateway_not_reloaded_or_auth_behaviour_wrong_blocks_the_lb(tmp_path, monkeypatch, failing):
    """A gateway that did not restart, or valid credentials refused / wrong ones accepted (the
    real requests of public_e2e and negative_tests), stops the campaign before any public LB."""
    state, ex, _ = run_campaign(tmp_path, monkeypatch, "mismatch", approve_edge_secret_sync=True, fail=(failing,))
    assert not lb_opened(ex) and "open-lb" not in state["phases"]


def test_password_never_reaches_commands_state_or_campaign_files(tmp_path, monkeypatch):
    cred = cred_file(tmp_path)
    state, ex, _ = run_campaign(tmp_path, monkeypatch, "mismatch", approve_edge_secret_sync=True, edge_credentials=str(cred))
    assert state["EDGE_AUTH"] == "EDGE_AUTH_SYNCED"
    assert all(PASSWORD not in c for c, _ in ex.cmds)
    campaign = Path(state["config"]["campaign_dir"])
    for f in campaign.rglob("*"):
        if f.is_file():
            assert PASSWORD not in f.read_text(errors="ignore"), f
    assert PASSWORD not in (tmp_path / "sim.jsonl").read_text()


# --- campaign: dns-transition scope -------------------------------------------------------------

def transition(tmp_path, monkeypatch=None, fail=(), **kw):
    args = dict(scope="dns-transition", mode="supervised", approve_dns_transition_test=True, dns_offline_verified=False)
    args.update(kw)
    path, state = ready_state(tmp_path, **args)
    return path, state


def test_transition_scope_has_its_own_gates_and_approval(tmp_path):
    _, state = transition(tmp_path)
    assert set(state["gates"]) == set(camp.TRANSITION_GATES)
    assert "SAFE_CLEANUP_READY" not in state["gates"] and "OWNER_LIVE_APPROVAL" not in state["gates"]
    assert all(v == "PASS" for v in state["gates"].values()), state["gates"]


@pytest.mark.parametrize("change,gate", [
    ({"approve_dns_transition_test": False}, "OWNER_DNS_TRANSITION_APPROVAL"),
    ({"mode": "unattended"}, "DNS_TRANSITION_TEST_READY"),
    ({"dns_offline_ipv6": None}, "DNS_TRANSITION_TEST_READY"),
    ({"dns_credentials": None}, "DNS_TRANSITION_TEST_READY"),
    ({"dns_offline_mode": "no-ipv4"}, "DNS_TRANSITION_TEST_READY"),
])
def test_transition_scope_refuses_missing_preconditions(tmp_path, change, gate):
    _, state = transition(tmp_path, **change)
    assert state["gates"][gate] == "FAIL"


def test_transition_needs_dns_proven_offline_now(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGE_SIM_FAIL", "dns_check.py")
    _, state = transition(tmp_path)
    assert state["gates"]["DNS_TRANSITION_TEST_READY"] == "FAIL"


def test_the_live_g3b_approval_does_not_unlock_the_transition_test(tmp_path):
    _, state = transition(tmp_path, approve_dns_transition_test=False, approve_live_campaign=True)
    assert state["gates"]["OWNER_DNS_TRANSITION_APPROVAL"] == "FAIL"


def test_successful_transition_cycles_dns_on_the_lb_then_cleans_up(tmp_path):
    path, _ = transition(tmp_path)
    ex = Recorder(tmp_path / "sim.jsonl")
    state = camp.run(path, ex)
    assert state["DNS_TRANSITION"] == "VERIFIED" and state["DNS_TARGET_STATE"] == "offline"
    names = [n for n, _ in camp.phases(state)]
    assert names[-3:] == ["private-e2e", "open-lb", "dns-transition"] and set(state["phases"]) == set(names)
    assert all(v == "PASS" for v in state["phases"].values())
    texts = [c for c, _ in ex.cmds]
    for absent in ("issue_certificate.sh --environment staging", "--confirm-production", "PUBLIC_INTERNET", "rootUrl"):
        assert not any(absent in c for c in texts), absent
    online = ex.index(f"online --ip {LB}")
    offline = ex.index("offline --mode signoff")
    assert f"--offline-ip {OFF4} --offline-ipv6 {OFF6} --released-ip {LB}" in texts[offline]
    order = [ex.index("07-private-negative.jsonl"), lb_index(ex), ex.index("--expect-offline"), online, offline]
    assert order == sorted(order)
    release = max(i for i, c in enumerate(texts) if "upgrade ewallet-edge-gateway" in c and "public-open" not in c)
    assert offline < release < ex.index("--scale-pdb-guarded")
    assert sum("dynu_dns.py" in c for c in texts) == 2  # cleanup has nothing left to take offline
    v = state["verdicts"]
    assert v["SCOPE"] == "dns-transition" and v["G3B_SECURITY_STATUS"].startswith("NOT_RUN")
    assert state["CLEANUP_STATUS"] == "COMPLETE" and state["SLEEP_STATUS"] == "VERIFIED"


def test_dns_not_offline_at_the_start_sends_no_update(tmp_path):
    path, _ = transition(tmp_path)
    ex = Recorder(tmp_path / "sim.jsonl", fail=("dns_check.py",))
    state = camp.run(path, ex)
    assert state["DNS_TRANSITION"].startswith("DNS_TRANSITION_NOT_STARTED")
    assert not any("dynu_dns.py" in c for c, _ in ex.cmds)
    assert state["cleanup_steps"]["lb-release"]["ok"]


def test_online_not_proven_is_taken_offline_by_cleanup_before_the_lb_is_released(tmp_path):
    path, _ = transition(tmp_path)
    ex = Recorder(tmp_path / "sim.jsonl", fail=(" online --ip",))
    state = camp.run(path, ex)
    assert state["DNS_TRANSITION"] == "DNS_TRANSITION_ONLINE_NOT_PROVEN"
    off = ex.index("offline --mode signoff")
    release = max(i for i, (c, _) in enumerate(ex.cmds) if "upgrade ewallet-edge-gateway" in c and "public-open" not in c)
    assert off < release and state["cleanup_steps"]["dns-offline"]["ok"]
    assert state["CLEANUP_STATUS"] == "COMPLETE"


def test_signoff_failure_keeps_the_lb_still_sleeps_and_resumes(tmp_path):
    path, _ = transition(tmp_path)
    ex = Recorder(tmp_path / "sim.jsonl", fail=("offline --mode signoff",))
    state = camp.run(path, ex)
    assert state["DNS_TRANSITION"] == "DNS_TRANSITION_OFFLINE_NOT_PROVEN"
    assert state["cleanup_steps"]["lb-release"]["detail"].startswith("LB KEPT")
    assert state["cleanup_steps"]["sleep"]["ok"] and state["CLEANUP_STATUS"] == "CLEANUP_INCOMPLETE"
    # owner fallback: panel "Go Offline Now", then cleanup again; DNS proven offline releases the LB
    later = camp.cleanup(path, Recorder(tmp_path / "sim2.jsonl"), "manual cleanup")
    assert later["CLEANUP_STATUS"] == "COMPLETE" and later["cleanup_steps"]["lb-release"]["ok"]


def test_propagation_never_completing_counts_as_not_proven(tmp_path):
    """dynu_dns exits 1 when dns_check stays NOT_YET for all attempts: same path as a failure."""
    path, _ = transition(tmp_path)

    class Slow(Recorder):
        def run(self, cmd, env=None, timeout=3600):
            if "dynu_dns.py" in " ".join(cmd) and " online " in " ".join(cmd):
                self.cmds.append((" ".join(cmd), []))
                return 1, json.dumps({"verdict": "DNS_ONLINE_NOT_YET", "dns_check_attempts": 10})
            return super().run(cmd, env, timeout)
    state = camp.run(path, Slow(tmp_path / "sim.jsonl"))
    assert state["DNS_TRANSITION"] == "DNS_TRANSITION_ONLINE_NOT_PROVEN"
    assert "DNS_ONLINE_NOT_YET" in (Path(state["config"]["campaign_dir"]) / "dns-transition-1-online.jsonl").read_text()


# --- G3-B scope: --dns-offline-verified needs the transition evidence -------------------------------

def test_a_bare_dns_offline_verified_flag_is_not_enough(tmp_path):
    _, state = ready_state(tmp_path, mode="supervised", approve_live_campaign=True, dns_transition_evidence=None)
    assert state["gates"]["SAFE_CLEANUP_READY"] == "FAIL"
    assert any("needs --dns-transition-evidence" in p for p in state["gate_problems"])


def test_evidence_without_the_owner_flag_does_not_enable_anything(tmp_path):
    _, state = ready_state(tmp_path, mode="supervised", approve_live_campaign=True, dns_offline_verified=False)
    assert state["gates"]["SAFE_CLEANUP_READY"] == "FAIL"


@pytest.mark.parametrize("change", [{"DNS_TRANSITION": "DNS_TRANSITION_OFFLINE_NOT_PROVEN"}, {"scope": "g3b"},
                                    {"config": {"domain": "edge.example.test", "dns_offline_mode": "signoff",
                                                "dns_offline_ip": "192.0.2.99", "dns_offline_ipv6": OFF6}}])
def test_evidence_must_prove_this_exact_configuration(tmp_path, change):
    evidence = tmp_path / "other-state.json"
    base = {"scope": "dns-transition", "DNS_TRANSITION": "VERIFIED",
            "config": {"domain": "edge.example.test", "dns_offline_mode": "signoff", "dns_offline_ip": OFF4, "dns_offline_ipv6": OFF6}}
    evidence.write_text(json.dumps({**base, **change}))
    _, state = ready_state(tmp_path, mode="supervised", approve_live_campaign=True, dns_transition_evidence=str(evidence))
    assert state["gates"]["SAFE_CLEANUP_READY"] == "FAIL"


def test_a_real_verified_transition_run_allows_the_normal_g3b_approval(tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    path, _ = transition(first)
    assert camp.run(path, Recorder(first / "sim.jsonl"))["DNS_TRANSITION"] == "VERIFIED"
    second = tmp_path / "second"
    second.mkdir()
    _, state = ready_state(second, mode="supervised", approve_live_campaign=True, dns_transition_evidence=str(path))
    assert state["gates"]["SAFE_CLEANUP_READY"] == "PASS" and all(v == "PASS" for v in state["gates"].values())


def test_a_failed_transition_run_does_not(tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    path, _ = transition(first)
    camp.run(path, Recorder(first / "sim.jsonl", fail=(" online --ip",)))
    second = tmp_path / "second"
    second.mkdir()
    _, state = ready_state(second, mode="supervised", approve_live_campaign=True, dns_transition_evidence=str(path))
    assert state["gates"]["SAFE_CLEANUP_READY"] == "FAIL"


def test_full_g3b_order_is_unchanged_apart_from_edge_auth(tmp_path, monkeypatch):
    state, ex, _ = run_campaign(tmp_path, monkeypatch)
    assert set(state["phases"]) == set(n for n, _ in camp.phases(state))
    assert [n for n, _ in camp.phases(state)] == ["wake", "baseline", "temporary-certificate", "edge-restricted", "L0-render", "L0-live",
                                     "edge-auth", "private-e2e", "open-lb", "dns-online", "internet-gates", "L4-denied",
                                     "staging-certificate", "production-certificate", "responder-off", "grafana", "public-e2e"]
    assert not any("dns-transition" in c for c, _ in ex.cmds)
