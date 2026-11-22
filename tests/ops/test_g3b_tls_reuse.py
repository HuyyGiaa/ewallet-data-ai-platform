"""Reusing the trusted certificate already in edge-tls instead of issuing a new one (after run 6
issued it). verify_edge_tls.py runs for real against a fake kubectl and throwaway test certificates;
the campaign runs with its Simulator. No real key material is involved."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.ops.test_dns_campaign import EDGE, Recorder, camp, ready_state
from tests.ops.test_g3b_supervised import Campaign, rows_for

DOMAIN = "edge.example.test"
NAMES = [f"{h}.{DOMAIN}" for h in ("features", "agents", "registry", "observe")]
FAKE_KUBECTL = """#!/usr/bin/env bash
[ -f "$FAKE_DIR/fail" ] && exit 1
cat "$FAKE_DIR/secret.json"
"""


def run(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, check=True, **kw)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    run("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "30",
        "-subj", "/CN=Test Root", "-keyout", str(d / "ca.key"), "-out", str(d / "ca.pem"),
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign")
    run("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "30",
        "-subj", "/CN=Other Root", "-keyout", str(d / "other.key"), "-out", str(d / "other.pem"))

    def leaf(name, sans, days):
        run("openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-subj", f"/CN={sans[0] if sans else 'x'}", "-keyout", str(d / f"{name}.key"), "-out", str(d / f"{name}.csr"))
        ext = d / f"{name}.ext"
        ext.write_text("subjectAltName=" + ",".join(f"DNS:{s}" for s in sans) + "\nextendedKeyUsage=serverAuth\n")
        run("openssl", "x509", "-req", "-in", str(d / f"{name}.csr"), "-CA", str(d / "ca.pem"), "-CAkey", str(d / "ca.key"),
            "-CAcreateserial", "-days", str(days), "-extfile", str(ext), "-out", str(d / f"{name}.pem"))
    leaf("good", NAMES, 30)
    leaf("short", NAMES, 5)
    leaf("fewer", NAMES[:3], 30)
    leaf("spare", NAMES, 30)
    return d


@pytest.fixture
def kube(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kubectl").write_text(FAKE_KUBECTL)
    (bin_dir / "kubectl").chmod(0o755)
    return tmp_path, {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_DIR": str(tmp_path)}


def secret(tmp, crt: bytes, key: bytes | None):
    data = {"tls.crt": base64.b64encode(crt).decode()}
    if key is not None:
        data["tls.key"] = base64.b64encode(key).decode()
    (tmp / "secret.json").write_text(json.dumps({"data": data}))


def verify(env, ca, min_days=14):
    return subprocess.run([sys.executable, str(EDGE / "verify_edge_tls.py"), "--domain", DOMAIN, "--context", "ctx",
                           "--min-days", str(min_days), "--ca-bundle", str(ca)], capture_output=True, text=True, env=env)


# --- verify_edge_tls.py --------------------------------------------------------------------------

def test_a_valid_trusted_certificate_is_reusable_and_no_key_material_is_printed(kube, pki):
    tmp, env = kube
    key = (pki / "good.key").read_bytes()
    secret(tmp, (pki / "good.pem").read_bytes(), key)
    done = verify(env, pki / "ca.pem")
    out = json.loads(done.stdout)
    assert done.returncode == 0 and out["verdict"] == "TLS_REUSE_VERIFIED" and set(NAMES) <= set(out["names"])
    assert "PRIVATE KEY" not in done.stdout + done.stderr and base64.b64encode(key).decode()[:40] not in done.stdout


@pytest.mark.parametrize("crt,key,ca,days,reason", [
    ("fewer", "fewer", "ca", 14, "missing names"),
    ("short", "short", "ca", 14, "expires within 14 days"),
    ("good", "good", "other", 14, "chain not trusted"),
    ("good", "spare", "ca", 14, "does not match"),
], ids=["missing-san", "near-expiry", "untrusted", "key-mismatch"])
def test_unsafe_certificates_are_refused(kube, pki, crt, key, ca, days, reason):
    tmp, env = kube
    secret(tmp, (pki / f"{crt}.pem").read_bytes(), (pki / f"{key}.key").read_bytes())
    done = verify(env, pki / f"{ca}.pem", days)
    assert done.returncode == 1 and json.loads(done.stdout)["verdict"] == "TLS_REUSE_REFUSED"
    assert reason in json.loads(done.stdout)["reason"] and "PRIVATE KEY" not in done.stdout


def test_missing_key_unreadable_or_missing_secret(kube, pki):
    tmp, env = kube
    secret(tmp, (pki / "good.pem").read_bytes(), None)
    assert verify(env, pki / "ca.pem").returncode == 1
    secret(tmp, b"not a certificate", b"x")
    assert verify(env, pki / "ca.pem").returncode == 1
    (tmp / "fail").write_text("")
    done = verify(env, pki / "ca.pem")
    assert done.returncode == 2 and json.loads(done.stdout)["verdict"] == "TLS_REUSE_CHECK_FAILED"


# --- campaign: reuse mode --------------------------------------------------------------------------

def reuse_state(tmp_path, **kw):
    return ready_state(tmp_path, mode="supervised", approve_live_campaign=True, reuse_existing_production_certificate=True, **kw)


def texts(ex):
    return [c for c, _ in ex.cmds]


def test_1_valid_certificate_is_reused_nothing_is_issued_or_imported(tmp_path):
    path, state = reuse_state(tmp_path)
    assert state["gates"]["TLS_REUSE_READY"] == "PASS" and "CERTBOT_READY" not in state["gates"]
    ex = Campaign(tmp_path / "s.jsonl", ips=("203.0.113.7", "198.51.100.200", "198.51.100.200", "203.0.113.7"), e2e_rows=rows_for)
    result = camp.run(path, ex)
    cmds = texts(ex)
    assert not any("issue_certificate.sh" in c or "certbot" in c or "create secret tls" in c for c in cmds)
    assert result["phases"]["tls-reuse-verify"] == "PASS" and "temporary-certificate" not in result["phases"]
    assert result["phases"]["staging-certificate"] == "SKIPPED_REUSE" and result["phases"]["production-certificate"] == "SKIPPED_REUSE"
    assert result["TLS_MODE"] == "REUSE_EXISTING" and result["TLS_CERTIFICATE_STATUS"] == "VERIFIED_TRUSTED"
    assert result["TLS_CERTIFICATE_SOURCE"] == "EXISTING_EDGE_TLS" and result["ACME_PRODUCTION_ATTEMPTS"] == 0
    assert result["ACME_STAGING"] == "SKIPPED_REUSE" and result["ACME_PRODUCTION"] == "SKIPPED_REUSE"
    assert result.get("ACME_PRODUCTION_FINAL_STATUS") != "ISSUED"
    edge = [c for c in cmds if "upgrade ewallet-edge " in c]
    assert edge and all("g3b-values_final.yaml" in c for c in edge)          # no ACME responder deployed
    assert result["phases"]["public-e2e"] == "PASS" and result["verdicts"]["G3B_DEPLOYMENT_STATUS"] == "PASS"
    assert result["verdicts"]["G3B_SECURITY_STATUS"] == "G3B_SECURITY_COMPLETE"
    assert result["verdicts"]["TLS_CERTIFICATE_STATUS"] == "VERIFIED_TRUSTED"


def test_6_reuse_never_relaxes_tls_verification(tmp_path):
    path, _ = reuse_state(tmp_path)
    ex = Campaign(tmp_path / "s.jsonl", e2e_rows=rows_for)
    camp.run(path, ex)
    tests = [c for c in texts(ex) if "public_e2e.py" in c or "negative_tests.py" in c]
    assert tests and not any("--ca-file" in c for c in tests)                  # system trust, real hostnames
    assert not any(" -k " in f" {c} " or "--insecure" in c for c in texts(ex))
    for script in ("public_e2e.py", "negative_tests.py", "verify_edge_tls.py"):
        source = (EDGE / script).read_text()
        assert "--insecure" not in source and "verify=False" not in source and '"-k"' not in source
    private = [c for c in tests if "--address 127.0.0.1" in c]
    assert private and all(f"--domain {DOMAIN}" in c for c in private)       # port-forward still uses the hostnames


@pytest.mark.parametrize("approved", [True, False])
def test_2_3_4_5_refused_certificate_fails_closed_without_fallback(tmp_path, monkeypatch, approved):
    monkeypatch.setenv("EDGE_SIM_TLS_REUSE", "refuse")
    path, state = reuse_state(tmp_path, approve_production_certificate=approved)
    assert state["gates"]["TLS_REUSE_READY"] == "FAIL"
    ex = Campaign(tmp_path / "s.jsonl", e2e_rows=rows_for)
    result = camp.run(path, ex)                                                # Simulator ignores failed gates
    assert result["phases"]["tls-reuse-verify"].startswith("FAIL: existing certificate not reusable")
    assert "(no fallback issuance)" in result["phases"]["tls-reuse-verify"]
    cmds = texts(ex)
    assert not any("issue_certificate.sh" in c or "create secret tls" in c for c in cmds)
    assert not any("upgrade ewallet-edge-gateway" in c and "public-open" in c for c in cmds)
    assert result["CLEANUP_STATUS"] == "COMPLETE" and result["SLEEP_STATUS"] == "VERIFIED"         # 8 cleanup reachable


def test_reuse_needs_the_production_certificate_approval(tmp_path):
    _, state = reuse_state(tmp_path, approve_production_certificate=False)
    assert state["gates"]["TLS_REUSE_READY"] == "FAIL"


def test_7_legacy_issuance_mode_is_unchanged(tmp_path):
    path, state = ready_state(tmp_path, mode="supervised", approve_live_campaign=True)
    assert "CERTBOT_READY" in state["gates"] and "TLS_REUSE_READY" not in state["gates"]
    ex = Campaign(tmp_path / "s.jsonl", e2e_rows=rows_for)
    result = camp.run(path, ex)
    cmds = texts(ex)
    assert any("issue_certificate.sh --environment self-signed" in c and "--import" in c for c in cmds)
    assert any("--environment staging" in c for c in cmds) and any("--environment production" in c for c in cmds)
    assert any("--ca-file" in c for c in cmds if "public_e2e.py" in c and "07-private" in c)
    assert result["phases"]["temporary-certificate"] == "PASS" and result["ACME_PRODUCTION_ATTEMPTS"] == 1
    assert result["verdicts"]["TLS_MODE"] == "ISSUE" and result["verdicts"]["TLS_CERTIFICATE_STATUS"] == "ISSUED"


def test_8_cleanup_after_a_late_failure_never_touches_edge_tls(tmp_path):
    path, _ = reuse_state(tmp_path)
    ex = Campaign(tmp_path / "s.jsonl", fail=("14-e2e.jsonl",), e2e_rows=rows_for)
    result = camp.run(path, ex)
    assert result["phases"]["public-e2e"].startswith("FAIL") and result["CLEANUP_STATUS"] == "COMPLETE"
    assert not any("issue_certificate.sh" in c or "create secret tls" in c or "delete secret" in c for c in texts(ex))


def test_9_grafana_readiness_still_precedes_final_public_e2e(tmp_path):
    path, state = reuse_state(tmp_path, approve_grafana_root_url=True)
    grafana = tmp_path / "grafana-cred"
    grafana.write_text("admin:synthetic")
    state["config"]["grafana_credentials"] = str(grafana)
    camp.save(path, state)
    ex = Campaign(tmp_path / "s.jsonl", e2e_rows=rows_for)
    result = camp.run(path, ex)
    cmds = texts(ex)
    rollout = next(i for i, c in enumerate(cmds) if "rollout status deployment/grafana" in c)
    final = next(i for i, c in enumerate(cmds) if "14-e2e.jsonl" in c)
    assert rollout < final and result["GRAFANA"] == "PASS"


def test_10_status_distinguishes_reuse_from_issuance(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    reuse_path, _ = reuse_state(tmp_path / "a")
    issue_path, _ = ready_state(tmp_path / "b", mode="supervised", approve_live_campaign=True)
    r = camp.run(reuse_path, Campaign(tmp_path / "a.jsonl", e2e_rows=rows_for))
    i = camp.run(issue_path, Campaign(tmp_path / "b.jsonl", e2e_rows=rows_for))
    assert (r["verdicts"]["TLS_MODE"], r["verdicts"]["TLS_CERTIFICATE_STATUS"]) == ("REUSE_EXISTING", "VERIFIED_TRUSTED")
    assert (i["verdicts"]["TLS_MODE"], i["verdicts"]["TLS_CERTIFICATE_STATUS"]) == ("ISSUE", "ISSUED")
    assert r["phases"]["production-certificate"] == "SKIPPED_REUSE" and i["phases"]["production-certificate"] == "PASS"
    assert r["verdicts"]["G3B_DEPLOYMENT_STATUS"] == i["verdicts"]["G3B_DEPLOYMENT_STATUS"] == "PASS"


def test_reuse_l0_render_uses_the_values_without_the_acme_responder(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _, reuse = reuse_state(tmp_path / "a")
    _, issue = ready_state(tmp_path / "b", mode="supervised", approve_live_campaign=True)
    def edge_render(root):
        cmds = [json.loads(l)["cmd"] for l in (root / "sim.jsonl").read_text().splitlines()]
        return next(" ".join(c) for c in cmds if "template" in c and "ewallet-edge" in c and "ewallet-edge-gateway" not in c)
    assert "g3b-values_final.yaml" in edge_render(tmp_path / "a") and "values_acme" not in edge_render(tmp_path / "a")
    assert "g3b-values_acme.yaml" in edge_render(tmp_path / "b")
    assert json.loads(Path(reuse["config"]["values_final"]).read_text())["acmeHttp01"]["enabled"] is False
