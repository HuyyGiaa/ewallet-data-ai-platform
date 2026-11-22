"""No credential may reach a recorded result, stdout or a failure message (G3-B 2026-11-22:
curl's %{redirect_url} carried the edge credentials into a Grafana redirect observation).
Synthetic credentials only."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.ops.test_edge_negative_checks import nt

ROOT = Path(__file__).resolve().parents[2]
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("public_e2e", ROOT / "scripts/edge/public_e2e.py")
pe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pe)

USER, PASSWORD = "edgeuser", "Synth3tic-Redaction-Pa55word-xyz"
CREDS = f"{USER}:{PASSWORD}"
DOMAIN = "edge.test"


def leaks(text: str) -> list[str]:
    forms = [PASSWORD, quote(PASSWORD, safe=""), base64.b64encode(CREDS.encode()).decode(), f"{USER}:"]
    return [f for f in forms if f in text]


@pytest.mark.parametrize("url,expected", [
    (f"https://{CREDS}@observe.{DOMAIN}/login", f"https://observe.{DOMAIN}/login"),                        # 1 absolute
    (f"https://{quote(USER)}:{quote(PASSWORD, safe='')}@observe.{DOMAIN}/login?x=1",
     f"https://observe.{DOMAIN}/login?x=1"),                                                                  # 2 encoded
    ("https://edge%40corp:p%3Ass%2Fword@observe.edge.test:8443/a?b=c#d", "https://observe.edge.test:8443/a?b=c#d"),
    (f"https://observe.{DOMAIN}/login", f"https://observe.{DOMAIN}/login"),                                 # 3 none
    (f"https://{CREDS}@evil.example/steal", "https://evil.example/steal"),                                   # 4 other host
    ("/login?redirect=%2F", "/login?redirect=%2F"),                                                           # 5 relative
    (f"https://{CREDS}@observe.{DOMAIN}:18443/login", f"https://observe.{DOMAIN}:18443/login"),             # 6 port
    (f"http://{CREDS}@[2001:db8::1]:8080/x", "http://[2001:db8::1]:8080/x"),
    ("https://observe.edge.test/path?mail=a@b.example", "https://observe.edge.test/path?mail=a@b.example"),
    ("", ""),
], ids=["absolute", "encoded", "encoded-port-fragment", "no-userinfo", "other-host", "relative", "port", "ipv6",
        "at-in-query", "empty"])
def test_redact_url_removes_only_userinfo(url, expected):
    assert nt.redact_url(url) == expected


def make_edge(tmp_path, http2=False):
    cred = tmp_path / "edge-cred"
    cred.write_text(CREDS + "\n")
    return nt.Edge(argparse.Namespace(address="127.0.0.1", https_port=18443, http_port=18080, domain=DOMAIN, ca_file=None,
                                      stub=False, foundation=False, rate_limit_mode="http2" if http2 else "threads",
                                      credentials_file=str(cred)))


def test_request_redacts_curls_redirect_url(tmp_path, monkeypatch):
    edge = make_edge(tmp_path)
    monkeypatch.setattr(nt.subprocess, "run", lambda *a, **k: argparse.Namespace(
        stdout=f"\nhttps://{CREDS}@observe.{DOMAIN}:18443/login\n302", returncode=0))
    reply = edge.request("observe", "/", authed=True)
    assert reply.status == 302 and reply.location == f"https://observe.{DOMAIN}:18443/login" and not leaks(reply.location)


def test_http2_burst_redacts_every_transfer_location(tmp_path, monkeypatch):          # 7
    edge = make_edge(tmp_path, http2=True)

    def fake_run(command, input, capture_output, text):
        outs = [command[i + 1] for i, a in enumerate(command) if a == "-o"]
        for out in outs:
            Path(out).write_text("")
        lines = [f"{out} 302 https://{CREDS}@registry.{DOMAIN}/next" for out in outs]
        return argparse.Namespace(stdout="\n".join(lines) + "\n", returncode=0)
    monkeypatch.setattr(nt.subprocess, "run", fake_run)
    replies, _ = edge.burst("registry", "/", 4)
    assert all(r.location == f"https://registry.{DOMAIN}/next" for r in replies)
    assert not any(leaks(r.location) for r in replies)


class GrafanaEdge:
    """Just enough of nt.Edge for grafana_checks."""

    def __init__(self, location):
        self.domain, self.location = DOMAIN, location

    def request(self, host, path, *extra, scheme="https", authed=False):
        return nt.Reply(302, "", nt.redact_url(self.location))

    def authed(self, host, path, *extra):
        return 200, "[]" if "type=dash-db" in path else '{"data": {"result": []}, "traces": []}'


@pytest.mark.parametrize("location,passed", [
    (f"https://{CREDS}@observe.{DOMAIN}/login", True),       # 8 Grafana login redirect, same host
    (f"https://{CREDS}@evil.example/login", False),          # 4 other host still fails the check
    ("/login", True),                                         # 5 relative stays on the host
], ids=["grafana-same-host", "other-host", "relative"])
def test_grafana_redirect_row_keeps_its_verdict_without_the_credential(location, passed):
    results = []
    pe.grafana_checks(GrafanaEdge(location), results, "admin:synthetic-grafana-secret", None, '{k8s_namespace_name="ewallet-agents"}')
    redirect = next(r for r in results if r["check"] == "Grafana redirects to its login on the same host")
    assert redirect["passed"] is passed and not leaks(redirect["observed"])


def test_sanitize_text_scrubs_every_secret_form_and_any_userinfo():
    secrets = nt.secret_forms(CREDS)
    text = (f"Location: https://{CREDS}@a.test/x | {PASSWORD} | {quote(PASSWORD, safe='')} | "
            f"Basic {base64.b64encode(CREDS.encode()).decode()} | ftp://other:secret@b.test/ | user {USER}")
    clean = nt.sanitize_text(text, secrets)
    assert not leaks(clean) and "other:secret@" not in clean and "https://a.test/x" in clean and f"user {USER}" in clean


def test_public_e2e_jsonl_and_stdout_never_carry_credentials(tmp_path, monkeypatch, capsys):      # 9, 10
    cred = tmp_path / "edge-cred"
    cred.write_text(CREDS + "\n")
    grafana = tmp_path / "grafana-cred"
    grafana.write_text("admin:synthetic-grafana-secret\n")

    def noisy(edge, results, *a, **k):
        results.append({"row": "37", "check": "noisy", "passed": False,
                        "observed": f"302 https://{CREDS}@observe.{DOMAIN}/ {PASSWORD} synthetic-grafana-secret"})
    for name in ("tls_checks", "feature_checks", "ui_checks", "grafana_checks"):
        monkeypatch.setattr(pe, name, noisy)
    out = tmp_path / "e2e.jsonl"
    rc = pe.main(["--address", "127.0.0.1", "--domain", DOMAIN, "--credentials-file", str(cred), "--grafana-credentials-file",
                  str(grafana), "--evidence-class", "GKE_PRIVATE", "--out", str(out)])
    stdout = capsys.readouterr().out
    assert rc == 1
    for text in (stdout, out.read_text()):
        assert not leaks(text) and "synthetic-grafana-secret" not in text and "<redacted>" in text
    assert all(json.loads(l)["observed"].startswith("302 https://observe.") for l in out.read_text().splitlines())


def test_negative_tests_failure_report_and_jsonl_never_carry_credentials(tmp_path, monkeypatch, capsys):   # 10
    cred = tmp_path / "edge-cred"
    cred.write_text(CREDS + "\n")
    monkeypatch.setattr(nt.Edge, "curl", lambda self, *a, **k: (401, ""))
    monkeypatch.setattr(nt, "run_auth", lambda edge, results: nt.check(
        results, "wrong password is rejected", False, f"https://{CREDS}@features.{DOMAIN}/ {PASSWORD}"))
    monkeypatch.setattr(nt, "run_routing", lambda edge, results: None)
    monkeypatch.setattr(nt, "run_rate_limit", lambda edge, results: None)
    out = tmp_path / "neg.jsonl"
    rc = nt.main(["--domain", DOMAIN, "--credentials-file", str(cred), "--out", str(out)])
    stdout = capsys.readouterr().out
    assert rc == 1 and "FAIL wrong password is rejected" in stdout
    for text in (stdout, out.read_text()):
        assert not leaks(text) and f"https://features.{DOMAIN}/" in text


def test_loki_default_selector_uses_the_otel_namespace_label():
    parser_default = [a for a in pe.main.__code__.co_consts if isinstance(a, str) and "ewallet-agents" in a]
    assert '{k8s_namespace_name="ewallet-agents"}' in parser_default
    assert '{namespace="ewallet-agents"}' not in (ROOT / "scripts/edge/public_e2e.py").read_text()


def test_loki_override_is_still_honoured(tmp_path, monkeypatch):
    cred = tmp_path / "edge-cred"
    cred.write_text(CREDS + "\n")
    grafana = tmp_path / "grafana-cred"
    grafana.write_text("admin:synthetic-grafana-secret\n")
    seen = {}
    for name in ("tls_checks", "feature_checks", "ui_checks"):
        monkeypatch.setattr(pe, name, lambda *a, **k: None)
    monkeypatch.setattr(pe, "grafana_checks", lambda edge, results, creds, root, query: seen.update(query=query))
    pe.main(["--address", "127.0.0.1", "--domain", DOMAIN, "--credentials-file", str(cred), "--grafana-credentials-file",
             str(grafana), "--evidence-class", "GKE_PRIVATE", "--loki-query", '{k8s_namespace_name="ml-platform"}'])
    assert seen["query"] == '{k8s_namespace_name="ml-platform"}'
    pe.main(["--address", "127.0.0.1", "--domain", DOMAIN, "--credentials-file", str(cred), "--grafana-credentials-file",
             str(grafana), "--evidence-class", "GKE_PRIVATE"])
    assert seen["query"] == '{k8s_namespace_name="ewallet-agents"}'
