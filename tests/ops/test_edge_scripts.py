"""Focused tests for the G3-B/G4 edge scripts. No cluster, no network: fakes stand in for kubectl,
curl, dig and openssl. Addresses are documentation ranges only."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
EDGE = ROOT / "scripts/edge"
sys.path.insert(0, str(EDGE))


def load(name):
    spec = importlib.util.spec_from_file_location(name, EDGE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pc, e2e, dns, nt = load("platform_checks"), load("public_e2e"), load("dns_check"), load("negative_tests")


# --- platform_checks ----------------------------------------------------------------------------

SNAPSHOT = {"backends": {f"{ns}/{n}": {"ready_replicas": 1, "restarts": 5, "ready_endpoints": 1} for ns, n in pc.BACKENDS},
            "callers": {"jenkins->registry": "HTTP/1.0 200 OK", "grafana->prometheus": "Prometheus Server is Ready.",
                        "grafana->loki": "ready", "grafana->tempo": "ready"}}


def test_judge_passes_a_healthy_snapshot_and_its_own_baseline():
    assert all(r["passed"] for r in pc.judge(SNAPSHOT, SNAPSHOT))


@pytest.mark.parametrize("change,failing", [
    (lambda s: s["backends"]["kagent/kagent-ui"].update(restarts=6), "backend kagent/kagent-ui"),
    (lambda s: s["backends"]["ewallet-agents/grafana"].update(ready_replicas=0), "backend ewallet-agents/grafana"),
    (lambda s: s["backends"]["ml-platform/feature-api"].update(ready_endpoints=0), "backend ml-platform/feature-api"),
    (lambda s: s["callers"].update({"jenkins->registry": "HTTP/1.0 503"}), "jenkins -> registry"),
    (lambda s: s["callers"].update({"grafana->loki": ""}), "grafana -> loki"),
])
def test_judge_reports_regressions_against_the_baseline(change, failing):
    current = json.loads(json.dumps(SNAPSHOT))
    change(current)
    assert [r["check"] for r in pc.judge(current, SNAPSHOT) if not r["passed"]] == [failing]


class FakeKube(pc.Kube):
    def __init__(self, responses):
        super().__init__("kind-test", runner=self.run)
        self.responses, self.calls = responses, []

    def run(self, cmd):
        self.calls.append(cmd)
        for key, value in self.responses.items():
            if key in " ".join(cmd):
                return value(cmd) if callable(value) else value
        return "{}"


def pods(*states):
    return json.dumps({"items": [{"metadata": {"name": "ui-1"}, "status": {"containerStatuses": [{"ready": r, "restartCount": n}]}}
                                 for r, n in states]})


def test_watch_passes_when_ready_and_restarts_stay_constant():
    kube = FakeKube({"get deploy": json.dumps({"spec": {"selector": {"matchLabels": {"app": "ui"}}}}),
                     "get pods": pods((True, 5)), "get events": json.dumps({"items": []})})
    assert pc.watch(kube, "kagent", "kagent-ui", 60, 15, sleep=lambda _: None, now=lambda: "2026-01-01T00:00:00Z")[0]["passed"]


@pytest.mark.parametrize("sequence,events", [
    ([(True, 5), (False, 5)], []),
    ([(True, 5), (True, 6)], []),
    ([(True, 5)], [{"reason": "Unhealthy", "involvedObject": {"name": "kagent-ui-abc"}, "lastTimestamp": "2026-01-01T00:01:00Z"}]),
])
def test_watch_fails_on_not_ready_restart_or_probe_event(sequence, events):
    states = iter(sequence * 4)
    kube = FakeKube({"get deploy": json.dumps({"spec": {"selector": {"matchLabels": {"app": "ui"}}}}),
                     "get pods": lambda _: pods(next(states)), "get events": json.dumps({"items": events})})
    assert not pc.watch(kube, "kagent", "kagent-ui", 30, 15, sleep=lambda _: None, now=lambda: "2026-01-01T00:00:00Z")[0]["passed"]


def envoy_dump(https=("envoy.filters.http.local_ratelimit", "envoy.filters.http.basic_auth", "envoy.filters.http.router"),
               retry=False, secret=True, buckets=None):
    buckets = buckets or {"feature-api": 100, "kagent-ui": 100, "agent-registry": 50, "grafana": 50}
    routes = [{"name": n, "route": {"retry_policy": {}} if retry else {},
               "typed_per_filter_config": {"envoy.filters.http.local_ratelimit": {"token_bucket": {"max_tokens": b}}}}
              for n, b in buckets.items()]
    listener = lambda port, names: {"active_state": {"listener": {"address": {"socket_address": {"port_value": port}},
                                                                  "filter_chains": [{"filters": [{"typed_config": {"http_filters": [{"name": n} for n in names]}}]}]}}}
    return {"configs": [{"dynamic_listeners": [listener(443, https), listener(80, ["envoy.filters.http.router"])]},
                        {"dynamic_route_configs": [{"route_config": {"virtual_hosts": [{"routes": routes}]}}]},
                        {"dynamic_active_secrets": [{"name": "kubernetes-gateway://ewallet-ingress/edge-tls"}] if secret else []}]}


def test_envoy_checks_pass_on_the_g3a1_shape():
    assert all(r["passed"] for r in pc.envoy_checks(envoy_dump(), pc.route_limits()))


@pytest.mark.parametrize("dump,failing", [
    (envoy_dump(https=("envoy.filters.http.basic_auth", "envoy.filters.http.local_ratelimit", "envoy.filters.http.router")), "443: rate limit, then Basic Auth, then router"),
    (envoy_dump(https=("envoy.filters.http.router",)), "443: rate limit, then Basic Auth, then router"),
    (envoy_dump(retry=True), "no route retries"),
    (envoy_dump(secret=False), "TLS secret edge-tls loaded"),
    (envoy_dump(buckets={"feature-api": 100, "kagent-ui": 100, "agent-registry": 500, "grafana": 50}), "per-route token buckets equal values.yaml"),
])
def test_envoy_checks_fail_on_drift(dump, failing):
    assert failing in [r["check"] for r in pc.envoy_checks(dump, pc.route_limits()) if not r["passed"]]


def test_envoy_checks_can_require_the_rbac_filter():
    assert not all(r["passed"] for r in pc.envoy_checks(envoy_dump(), pc.route_limits(), restricted=True))


def test_gateway_checks_judge_conditions_and_grants():
    ok = {"status": {"conditions": [{"type": "Accepted", "status": "True"}, {"type": "Programmed", "status": "True"}],
                     "listeners": [{"name": n, "conditions": [{"type": "Accepted", "status": "True"}, {"type": "ResolvedRefs", "status": "True"}]}
                                   for n in ("https", "http")]}}
    route = {"metadata": {"name": "feature-api"}, "status": {"parents": [{"conditions": [
        {"type": "Accepted", "status": "True"}, {"type": "ResolvedRefs", "status": "True"}]}]}}
    grant = {"metadata": {"namespace": "ml-platform", "name": "g"},
             "spec": {"from": [{"namespace": "ewallet-ingress"}], "to": [{"kind": "Service", "name": "feature-api"}]}}
    assert all(r["passed"] for r in pc.gateway_checks(ok, ok, [route], [grant], True))
    assert not pc.gateway_checks(ok, ok, [route], [grant], False)[2]["passed"]  # Programmed while expecting 0 pods
    wide = {**grant, "spec": {**grant["spec"], "to": [{"kind": "Service"}, {"kind": "Service"}]}}
    assert not pc.gateway_checks(ok, ok, [route], [wide], True)[-1]["passed"]


def test_sleep_feasibility_reproduces_the_g3a1_gate():
    pdb = lambda ns, n: {"metadata": {"namespace": ns, "name": n}, "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app": n}}}}
    dep = lambda ns, n: {"metadata": {"namespace": ns, "name": n}, "spec": {"template": {"metadata": {"labels": {"app": n}}}}}
    keys = [("ewallet-agents", "fraud-coordinator"), ("ewallet-mcp", "drift-mcp"), ("ewallet-mcp", "feature-rag-mcp")]
    recorded = {f"{ns}/{n}": 2 for ns, n in keys}
    got = pc.predict_blockers([pdb(*k) for k in keys], [dep(*k) for k in keys], recorded,
                              {"ewallet-agents/fraud-coordinator", "ewallet-mcp/drift-mcp"})
    assert got == {"predicted_blockers": sorted(f"{ns}/{n}" for ns, n in keys), "not_covered": ["ewallet-mcp/feature-rag-mcp"]}
    assert pc.predict_blockers([pdb(*keys[2])], [dep(*keys[2])], {"ewallet-mcp/feature-rag-mcp": 0}, set())["predicted_blockers"] == []


# --- public_e2e ---------------------------------------------------------------------------------

class FakeEdge(nt.Edge):
    def __init__(self, routes, domain="edge.example.test"):
        self.domain, self.stub, self.foundation, self.http2, self.routes, self.seen = domain, False, False, False, routes, []
        self.address, self.https, self.ca = "203.0.113.10", 443, None

    def request(self, host, path, *extra, scheme="https", authed=False):
        self.seen.append((host, path, extra))
        for (h, prefix), reply in self.routes.items():
            if h == host and path.startswith(prefix):
                return reply(extra) if callable(reply) else reply
        return nt.Reply(404, "", "")


HTML = '<html><script src="/_next/static/chunks/app.js"></script></html>'
GRAFANA_LOGIN = '<html>{"appUrl":"https://observe.edge.example.test/"}<script src="public/build/runtime.js"></script></html>'


def app_routes(agents=nt.Reply(200, HTML, "")):
    return {
        ("features", nt.LOOKUP): nt.Reply(422, json.dumps({"error": {"code": "invalid_request", "correlation_id": "c1"}}), ""),
        ("agents", "/api/agents"): nt.Reply(200, json.dumps({"data": [1, 2, 3, 4]}), ""),
        ("agents", "/_next"): nt.Reply(200, "js", ""),
        ("agents", "/"): agents,
        ("registry", "/v0/agents"): nt.Reply(200, json.dumps([1]), ""),
        ("registry", "/_next"): nt.Reply(200, "js", ""),
        ("registry", "/"): nt.Reply(200, HTML, ""),
        ("observe", "/login"): nt.Reply(200, GRAFANA_LOGIN, ""),
        ("observe", "/api/search"): nt.Reply(200, json.dumps([{"uid": "a"}]), ""),
        ("observe", "/api/datasources/proxy/uid/prometheus"): nt.Reply(200, json.dumps({"data": {"result": [{"v": 1}]}}), ""),
        ("observe", "/api/datasources/proxy/uid/loki"): nt.Reply(200, json.dumps({"data": {"result": [{"values": [[1, "l"]]}]}}), ""),
        ("observe", "/api/datasources/proxy/uid/tempo"): nt.Reply(200, json.dumps({"traces": [{"id": 1}]}), ""),
        ("observe", "/logout"): nt.Reply(302, "", ""),
        ("observe", "/"): nt.Reply(302, "", "https://observe.edge.example.test/login"),
    }


def run_all(edge, agents="allowed"):
    results = []
    e2e.feature_checks(edge, results, None)
    e2e.ui_checks(edge, results, "40", "agents", "/api/agents", agents)
    e2e.ui_checks(edge, results, "41", "registry", "/v0/agents")
    e2e.grafana_checks(edge, results, "admin:synthetic", "https://observe.edge.example.test/", '{namespace="x"}')
    return results


def test_public_e2e_rows_pass_on_real_answers():
    results = run_all(FakeEdge(app_routes()))
    assert results and all(r["passed"] for r in results)
    assert {r["row"] for r in results} == {"37", "38", "39", "40", "41", "43"}


@pytest.mark.parametrize("key,reply,row", [
    (("features", nt.LOOKUP), nt.Reply(500, "", ""), "43"),
    (("observe", "/api/datasources/proxy/uid/loki"), nt.Reply(200, json.dumps({"data": {"result": []}}), ""), "38"),
    (("observe", "/api/datasources/proxy/uid/tempo"), nt.Reply(200, json.dumps({"traces": []}), ""), "39"),
    (("observe", "/api/datasources/proxy/uid/prometheus"), nt.Reply(502, "", ""), "37"),
    (("observe", "/"), nt.Reply(302, "", "http://localhost:3000/login"), "37"),
    (("registry", "/"), nt.Reply(200, "not html", ""), "41"),
])
def test_public_e2e_fails_on_wrong_or_empty_answers(key, reply, row):
    routes = app_routes()
    routes[key] = reply
    assert row in {r["row"] for r in run_all(FakeEdge(routes)) if not r["passed"]}


def test_public_e2e_agents_denied_mode():
    results = []
    e2e.ui_checks(FakeEdge(app_routes(nt.Reply(403, "RBAC: access denied", ""))), results, "40", "agents", "/api/agents", "denied")
    assert results[0]["passed"]
    results = []
    e2e.ui_checks(FakeEdge(app_routes()), results, "40", "agents", "/api/agents", "denied")
    assert not results[0]["passed"]


def test_public_e2e_grafana_password_never_reaches_a_command_line():
    edge = FakeEdge(app_routes())
    e2e.grafana_checks(edge, [], "admin:pw-synthetic-1234", None, '{namespace="x"}')
    assert "pw-synthetic-1234" not in json.dumps(edge.seen)


def test_public_e2e_tls_checks_use_the_reported_names():
    edge = FakeEdge({})
    cert = "subject=CN=x\nX509v3 Subject Alternative Name:\n    " + ", ".join(f"DNS:{h}.edge.example.test" for h in e2e.HOSTS)

    def run(cmd, input, capture_output, text):
        if "x509" in cmd:
            return SimpleNamespace(stdout=cert)
        if "-tls1_1" in cmd:
            return SimpleNamespace(stdout="New, (NONE), Cipher is (NONE)")
        return SimpleNamespace(stdout="Verify return code: 0 (ok)")
    results = []
    e2e.tls_checks(edge, results, run=run)
    assert all(r["passed"] for r in results) and len(results) == 3


def test_credential_files_must_be_outside_the_repository():
    with pytest.raises(SystemExit):
        e2e.private_file(str(ROOT / "README.md"))


# dns_check.py is tested in tests/ops/test_dns_campaign.py (fail-closed version).


# --- shell scripts: guards that act before any cluster or network call -----------------------

def sh(script, *args, env=None):
    return subprocess.run(["bash", str(EDGE / script), *args], capture_output=True, text=True, env={**os.environ, **(env or {})})


@pytest.mark.parametrize("args,message", [
    ([], "--domain is required"),
    (["--domain", "edge.example.test"], "--context is required"),
    (["--domain", "edge.example.test", "--context", "c"], "--private-dir is required"),
    (["--domain", "edge.example.test", "--context", "c", "--private-dir", str(ROOT / "tmp-private")], "outside the repository"),
    (["--domain", "edge.example.test", "--context", "c", "--private-dir", "{tmp}", "--environment", "production"], "--confirm-production"),
    (["--domain", "edge.example.test", "--context", "c", "--private-dir", "{tmp}", "--environment", "prod"], "staging, production or self-signed"),
])
def test_issue_certificate_refuses_before_any_acme_call(tmp_path, args, message):
    done = sh("issue_certificate.sh", *[a.replace("{tmp}", str(tmp_path / "p")) for a in args])
    assert done.returncode == 2 and message in done.stderr
    shutil.rmtree(ROOT / "tmp-private", ignore_errors=True)


@pytest.mark.parametrize("token,domain", [("../etc", "features.edge.example.test"), ("ok_token", "evil;domain")])
def test_acme_hook_refuses_bad_tokens_and_domains(token, domain):
    done = sh("acme_http01_hook.sh", "auth", env={"EDGE_ACME_CONTEXT": "kind-x", "CERTBOT_DOMAIN": domain,
                                                   "CERTBOT_TOKEN": token, "CERTBOT_VALIDATION": "v"})
    assert done.returncode == 2


def test_staged_rollout_check_refuses_a_non_kind_context(tmp_path):
    done = sh("staged_rollout_check.sh", str(tmp_path), env={"EDGE_LOCAL_CONTEXT": "gke_project_zone_cluster"})
    assert done.returncode == 2 and "non-kind" in done.stderr


def _grafana_image_present():
    import yaml
    image = yaml.safe_load((ROOT / "infra/helm/llm-agent-platform/values.yaml").read_text())["observabilityStack"]["grafana"]["image"]
    return shutil.which("docker") and subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


@pytest.mark.skipif(not _grafana_image_present(), reason="pinned Grafana image not available locally")
def test_grafana_root_url_check_against_the_pinned_image():
    done = sh("grafana_root_url_check.sh", "https://observe.edge.example.test/")
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count('"passed": true') == 2


# --- release_diff (frozen-chart upgrade gate) ----------------------------------------------------

rd = load("release_diff")


def grafana(env):
    return {"kind": "Deployment", "metadata": {"namespace": "ewallet-agents", "name": "grafana"},
            "spec": {"template": {"spec": {"containers": [{"name": "grafana", "env": env}]}}}}


BASE_ENV = [{"name": "GF_PATHS_DATA", "value": "/tmp/grafana-data"}]
ROOT_URL = "https://observe.edge.example.test/"
other = {"kind": "Deployment", "metadata": {"namespace": "ewallet-agents", "name": "prometheus"}, "spec": {"replicas": 1}}


def objs(*docs):
    return {(d["kind"], d["metadata"]["namespace"], d["metadata"]["name"]): d for d in docs}


def test_release_diff_accepts_exactly_the_approved_root_url():
    before = objs(grafana(BASE_ENV), other)
    after = objs(grafana(BASE_ENV + [{"name": "GF_SERVER_ROOT_URL", "value": ROOT_URL}]), other)
    assert rd.diff(before, after, ROOT_URL) == ([], [f"Deployment/ewallet-agents/grafana: GF_SERVER_ROOT_URL={ROOT_URL}"])
    assert rd.diff(before, after, None)[0]                                  # not approved
    assert rd.diff(before, after, "https://other.example.test/")[0]        # a different URL


@pytest.mark.parametrize("after", [
    objs(grafana(BASE_ENV + [{"name": "GF_SERVER_ROOT_URL", "value": ROOT_URL}, {"name": "X", "value": "1"}]), other),
    objs(grafana(BASE_ENV + [{"name": "GF_SERVER_ROOT_URL", "value": ROOT_URL}]), {**other, "spec": {"replicas": 2}}),
    objs(grafana(BASE_ENV + [{"name": "GF_SERVER_ROOT_URL", "value": ROOT_URL}])),
])
def test_release_diff_refuses_anything_else(after):
    problems, _ = rd.diff(objs(grafana(BASE_ENV), other), after, ROOT_URL)
    assert problems


def test_issue_certificate_self_signed_mode_writes_only_to_the_private_dir(tmp_path):
    done = sh("issue_certificate.sh", "--domain", "edge.example.test", "--context", "kind-x",
              "--private-dir", str(tmp_path / "p"), "--environment", "self-signed")
    assert done.returncode == 0, done.stderr
    cert = tmp_path / "p/self-signed/fullchain.pem"
    sans = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"], capture_output=True, text=True).stdout
    assert all(f"DNS:{h}.edge.example.test" in sans for h in ("features", "agents", "registry", "observe"))
    assert oct((tmp_path / "p").stat().st_mode)[-3:] == "700"
    assert oct((tmp_path / "p/self-signed/privkey.pem").stat().st_mode)[-3:] == "600"
    assert "BEGIN" not in done.stdout
