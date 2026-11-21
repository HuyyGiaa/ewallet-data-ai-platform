"""The edge security checks must fail on wrong answers, not only pass on right ones."""

from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("negative_tests", ROOT / "scripts/edge/negative_tests.py")
nt = importlib.util.module_from_spec(_spec)
sys.modules["negative_tests"] = nt  # dataclasses look the module up
_spec.loader.exec_module(nt)

DOMAIN = "edge.test"
ECHO = nt.Reply(200, '{"headers": {"host": "x"}}', "")
LIMITED = nt.Reply(429, "local_rate_limited", "")
DENIED = nt.Reply(401, "", "")
FAILURES = [nt.Reply(0, "", ""), DENIED, LIMITED, nt.Reply(500, '{"headers": {}}', "")]


class FakeEdge(nt.Edge):
    """An Edge whose replies come from a script instead of curl."""

    def __init__(self, respond, stub: bool = True, foundation: bool = False) -> None:
        self.domain, self.stub, self.foundation, self.respond = DOMAIN, stub, foundation, respond

    def request(self, host, path, *extra, scheme="https", authed=False):
        return self.respond(host, path, authed, scheme)


def rate_limit_scenario(normal=ECHO, burst=None, anonymous=DENIED, other_route=ECHO, recovery=ECHO):
    """Registry: one normal request, a burst of 120, then the recovery request."""
    calls, lock = [0], threading.Lock()
    burst = burst or (lambda n: ECHO if n % 2 else LIMITED)

    def respond(host, path, authed, scheme):
        if host == "registry" and authed:
            with lock:
                calls[0] += 1
                n = calls[0]
            return normal if n == 1 else burst(n) if n <= 121 else recovery
        return anonymous if host == "registry" else other_route
    return respond


def run_rate_limit(monkeypatch, **scenario) -> dict[str, bool]:
    monkeypatch.setattr(nt.time, "sleep", lambda _: None)
    results: list = []
    nt.run_rate_limit(FakeEdge(rate_limit_scenario(**scenario)), results)
    return {r.name: r.passed for r in results}


def test_rate_limit_checks_pass_on_the_expected_answers(monkeypatch):
    assert all(run_rate_limit(monkeypatch).values())


@pytest.mark.parametrize("reply", FAILURES, ids=lambda r: str(r.status))
def test_another_route_must_reach_its_backend(monkeypatch, reply):
    verdicts = run_rate_limit(monkeypatch, other_route=reply)
    assert verdicts["the limit is per route: another route still reaches its backend"] is False


@pytest.mark.parametrize("reply", FAILURES, ids=lambda r: str(r.status))
def test_recovery_must_reach_the_backend(monkeypatch, reply):
    verdicts = run_rate_limit(monkeypatch, recovery=reply)
    assert verdicts["the route recovers after the burst: the backend answers again"] is False


@pytest.mark.parametrize("bad", [0, 500, 503, 401], ids=str)
def test_a_burst_with_failures_is_not_rate_limiting(monkeypatch, bad):
    verdicts = run_rate_limit(monkeypatch, burst=lambda n: nt.Reply(bad, "", "") if n == 7 else LIMITED if n % 2 else ECHO)
    assert verdicts["a burst receives HTTP 429"] is False


def test_a_burst_that_is_all_429_proves_nothing_was_served(monkeypatch):
    assert run_rate_limit(monkeypatch, burst=lambda n: LIMITED)["a burst receives HTTP 429"] is False


@pytest.mark.parametrize("status", [0, 200, 403, 500], ids=str)
def test_an_unauthenticated_burst_may_only_see_401_or_429(monkeypatch, status):
    verdicts = run_rate_limit(monkeypatch, anonymous=nt.Reply(status, "", ""))
    assert verdicts["an unauthenticated burst is refused with 401 or 429 only"] is False


def test_an_unauthenticated_burst_of_401_and_429_passes(monkeypatch):
    assert run_rate_limit(monkeypatch, anonymous=LIMITED)["an unauthenticated burst is refused with 401 or 429 only"]


@pytest.mark.parametrize("status,body", [(0, ""), (401, ""), (429, ""), (500, '{"headers": {}}'), (200, "not json"), (200, '{"ok": 1}')])
def test_only_the_stub_echo_counts_as_reaching_the_application(status, body):
    assert nt.reached_application(FakeEdge(None), status, body) is False
    assert nt.registry_served(FakeEdge(None), nt.Reply(status, body, "")) is False
    assert nt.reached_application(FakeEdge(None), 200, ECHO.body) is True


@pytest.mark.parametrize("reply,served", [
    (nt.Reply(200, "<html>", ""), True),
    (nt.Reply(302, "", f"https://registry.{DOMAIN}/ui/"), True),
    (nt.Reply(308, "", f"https://registry.{DOMAIN}:31443/login"), True),
    (nt.Reply(302, "", f"http://registry.{DOMAIN}/ui/"), False),      # downgrade to HTTP
    (nt.Reply(302, "", "https://evil.example/ui/"), False),            # another host
    (nt.Reply(302, "", f"https://registry.{DOMAIN}.evil.example/"), False),
    (nt.Reply(304, "", ""), False),
    (nt.Reply(500, "", ""), False),
    (nt.Reply(0, "", ""), False),
], ids=lambda v: str(v) if isinstance(v, bool) else f"{v.status} {v.location}")
def test_the_real_registry_may_redirect_only_to_itself_over_https(reply, served):
    assert nt.registry_served(FakeEdge(None, stub=False), reply) is served


def test_the_stub_registry_must_not_redirect():
    assert nt.registry_served(FakeEdge(None), nt.Reply(302, "", f"https://registry.{DOMAIN}/")) is False


@pytest.mark.parametrize("anonymous,authed,passed", [(401, 404, True), (0, 404, False), (401, 0, False), (500, 404, False),
                                                     (401, 200, False), (404, 404, False)])
def test_an_undeclared_host_needs_the_edge_to_answer(monkeypatch, anonymous, authed, passed):
    def respond(host, path, is_authed, scheme):
        if scheme == "http":
            return nt.Reply(301, "", f"https://{host}.{DOMAIN}{path}")
        if host in ("features",):
            return nt.Reply(404, "", "")
        return nt.Reply(authed if is_authed else anonymous, "", "")
    results: list = []
    nt.run_routing(FakeEdge(respond), results)
    hosts = [r.passed for r in results if r.name.startswith("host ")]
    assert hosts and all(p is passed for p in hosts)
    assert {r.name: r.passed for r in results}["the redirect points to HTTPS on the same host"]


PASSWORDS = ['plain-password-without-specials', 'with"double"quotes', 'back\\slash\\', "it's an apostrophe",
             ' leading and trailing spaces ', 'all \\" \' \\\\ "mixed\\']


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not installed")
@pytest.mark.parametrize("password", PASSWORDS)
def test_curl_sends_the_password_unchanged(password):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(self.headers.get("authorization", ""))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    try:
        subprocess.run(["curl", "-sS", "-m", "5", "-o", "/dev/null", "-K", "-", f"http://127.0.0.1:{server.server_port}/"],
                       input=nt.curl_user_config(f"edge:{password}"), text=True, check=True, capture_output=True)
    finally:
        server.server_close()
    scheme, _, token = seen[0].partition(" ")
    assert scheme == "Basic" and base64.b64decode(token).decode() == f"edge:{password}"


@pytest.mark.parametrize("credentials", ["edge:pass\nword", "edge:pass\rword", "no-colon"])
def test_a_credential_curl_cannot_express_is_refused(credentials):
    with pytest.raises(ValueError):
        nt.curl_user_config(credentials)
