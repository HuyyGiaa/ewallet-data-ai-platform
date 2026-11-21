"""Public-exposure preflight (L0): an Internet-open edge LB is accepted only when the edge
controls hold exactly as approved. Real helm renders, documentation addresses only."""

from __future__ import annotations

import copy
import importlib.util
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EDGE = ROOT / "infra/helm/ewallet-edge"
DOMAIN = "edge.example.test"
OWNER = "203.0.113.7/32"          # documentation address (TEST-NET-3), never a real one
LB = frozenset({"ewallet-ingress/ewallet-edge-gateway"})
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm unavailable")

_spec = importlib.util.spec_from_file_location("ownership_preflight", ROOT / "scripts/edge/ownership_preflight.py")
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)


def helm(name, chart, *args):
    done = subprocess.run(["helm", "template", name, str(chart), "-n", "ewallet-ingress", *args],
                          capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(done.stdout) if d]


def gateway(*overlays):
    archive = Path(subprocess.run(["helm", "env", "HELM_REPOSITORY_CACHE"], capture_output=True, text=True).stdout.strip()) / "gateway-1.30.7.tgz"
    if not archive.exists():
        pytest.skip("istio/gateway 1.30.7 chart not cached")
    files = [x for o in ("values-gateway.yaml", "values-gateway-public.yaml", "values-gateway-public-open.yaml", *overlays)
             for x in ("-f", str(EDGE / o))]
    return helm("ewallet-edge-gateway", archive, *files)


ROUTES = [x for r in ("feature-api", "kagent-ui", "agent-registry", "grafana") for x in ("--set", f"routes.{r}.enabled=true")]


def edge(*extra, cidrs=(OWNER,), restrict=True):
    args = ["--set", "enabled=true", "--set", f"domain={DOMAIN}", "--set", "securityFiltersVerified=true", *ROUTES]
    if restrict:
        args += ["--set", "sourceRestriction.enabled=true", "--set-json", f"sourceRestriction.allowedCidrs={json.dumps(list(cidrs))}"]
    return helm("ewallet-edge", EDGE, *args, *extra)


def problems(docs, approved=(OWNER,), domain=DOMAIN, lb=LB):
    return pf.render_problems(docs, lb, True, domain, {ipaddress.ip_network(c) for c in approved} if approved else None)


def find(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


@pytest.fixture(scope="module")
def gw():
    return gateway()


@pytest.fixture(scope="module")
def secure():
    return edge()


def test_ps01_valid_public_configuration_passes(gw, secure):
    assert problems(gw + secure) == []


@pytest.mark.parametrize("filter_name", ["envoy.filters.http.basic_auth", "envoy.filters.http.local_ratelimit"])
def test_ps02_ps03_missing_basic_auth_or_rate_limit_fails(gw, secure, filter_name):
    docs = copy.deepcopy(secure)
    envoy = find(docs, "EnvoyFilter", "edge-security")
    envoy["spec"]["configPatches"] = [p for p in envoy["spec"]["configPatches"] if p.get("patch", {}).get("value", {}).get("name") != filter_name]
    assert any(filter_name in p for p in problems(gw + docs))


def test_ps03b_filter_on_the_wrong_listener_fails(gw, secure):
    docs = copy.deepcopy(secure)
    for patch in find(docs, "EnvoyFilter", "edge-security")["spec"]["configPatches"]:
        if patch.get("applyTo") == "HTTP_FILTER":
            patch["match"]["listener"]["portNumber"] = 80
    assert any("HTTPS listener" in p for p in problems(gw + docs))


def test_ps04_missing_source_restriction_fails(gw):
    assert any("edge-source-restriction is missing" in p for p in problems(gw + edge(restrict=False)))


@pytest.mark.parametrize("target", ["AuthorizationPolicy", "EnvoyFilter"])
def test_ps05_wrong_gateway_selector_fails(gw, secure, target):
    docs = copy.deepcopy(secure)
    obj = find(docs, target, "edge-source-restriction" if target == "AuthorizationPolicy" else "edge-security")
    key = ("spec", "selector", "matchLabels") if target == "AuthorizationPolicy" else ("spec", "workloadSelector", "labels")
    obj[key[0]][key[1]][key[2]] = {"istio": "some-other-gateway"}
    assert any("select" in p for p in problems(gw + docs))


def test_ps05b_policy_without_the_gateway_deployment_cannot_be_proven(secure):
    assert any("cannot prove" in p for p in problems(secure))


def test_ps06_wrong_hostname_fails(gw):
    old_domain = edge("--set", "domain=old.example.test")  # rendered for another domain
    assert any("agents." in p for p in problems(gw + old_domain))


@pytest.mark.parametrize("ports", [None, ["80"], ["8443"], ["443", "80"]])
def test_ps07_wrong_listener_port_fails(gw, secure, ports):
    docs = copy.deepcopy(secure)
    op = find(docs, "AuthorizationPolicy", "edge-source-restriction")["spec"]["rules"][0]["to"][0]["operation"]
    if ports is None:
        op.pop("ports")
    else:
        op["ports"] = ports
    assert any("ports" in p for p in problems(gw + docs))


def test_ps08_empty_cidr_list_fails(gw, secure):
    docs = copy.deepcopy(secure)
    find(docs, "AuthorizationPolicy", "edge-source-restriction")["spec"]["rules"][0]["from"][0]["source"]["notIpBlocks"] = []
    assert any("notIpBlocks is empty" in p for p in problems(gw + docs))


def test_ps09_public_cidr_not_approved_fails(gw):
    assert any("differ from the approved" in p for p in problems(gw + edge(cidrs=(OWNER, "198.51.100.0/24"))))


@pytest.mark.parametrize("cidr", ["999.999.999.999/32", "203.0.113.7/33", "203.0.113.7", "2001:db8::/32", "203.0.113.7/24", "abc"])
def test_ps10_invalid_ipv4_cidr_fails(cidr):
    networks, errors = pf.cidr_problems([cidr])
    assert errors and not networks


@pytest.mark.parametrize("cidr", ["10.20.0.31/32", "172.16.5.0/24", "192.168.1.0/24", "100.64.0.0/16", "169.254.7.127/32",
                                  "127.0.0.1/32", "8.0.0.0/8", "0.0.0.0/0"])
def test_ps11_ps12_private_shared_wide_or_catch_all_fails(gw, secure, cidr):
    networks, errors = pf.cidr_problems([cidr])
    assert errors and not networks
    docs = copy.deepcopy(secure)  # also when someone edits the rendered policy by hand
    find(docs, "AuthorizationPolicy", "edge-source-restriction")["spec"]["rules"][0]["from"][0]["source"]["notIpBlocks"] = [cidr]
    assert any("notIpBlocks" in p for p in problems(gw + docs))


def test_ps13_agents_route_disabled_passes_without_restriction(gw):
    assert problems(gw + edge("--set", "routes.kagent-ui.enabled=false", restrict=False), approved=None) == []


def test_ps15_valid_owner_cidr_matches_and_no_approval_means_no_pass(gw, secure):
    assert problems(gw + secure, approved=(OWNER,)) == []
    assert any("no approved CIDRs" in p for p in problems(gw + secure, approved=None))


def test_ps16_different_live_cidr_fails(gw, secure):
    assert any("differ from the approved" in p for p in problems(gw + secure, approved=("198.51.100.9/32",)))


def test_ps16b_a_narrowing_condition_fails(gw, secure):
    docs = copy.deepcopy(secure)
    rule = find(docs, "AuthorizationPolicy", "edge-source-restriction")["spec"]["rules"][0]
    rule["to"][0]["operation"]["paths"] = ["/api/*"]       # would leave the UI unrestricted
    rule["when"] = [{"key": "request.headers[x]", "values": ["y"]}]
    found = problems(gw + docs)
    assert any("narrows" in p for p in found) and any("hosts and ports" in p for p in found)


def test_ps17_external_traffic_policy_cluster_fails(secure):
    gw = gateway()
    find(gw, "Service", "ewallet-edge-gateway")["spec"]["externalTrafficPolicy"] = "Cluster"
    assert any("externalTrafficPolicy" in p for p in problems(gw + secure))


def test_ps18_unapproved_public_lb_fails(gw, secure):
    assert any("LoadBalancer Service" in p for p in pf.render_problems(gw + secure, frozenset(), False))
    extra = copy.deepcopy(find(gw, "Service", "ewallet-edge-gateway"))
    extra["metadata"]["name"] = "another-public-lb"
    assert any("another-public-lb" in p for p in problems(gw + secure + [extra]))


def test_ps19_acme_route_to_an_unexpected_backend_fails(gw):
    docs = edge("--set", "acmeHttp01.enabled=true")
    find(docs, "HTTPRoute", "acme-http01")["spec"]["rules"][0]["backendRefs"] = [{"name": "kagent-ui", "port": 8080}]
    assert any("acme-http01" in p for p in problems(gw + docs))


def test_ps19b_an_application_route_on_port_80_fails(gw, secure):
    docs = copy.deepcopy(secure)
    find(docs, "HTTPRoute", "feature-api")["spec"]["parentRefs"] = [{"name": "ewallet-edge", "sectionName": "http"}]
    assert any("HTTPS listener" in p for p in problems(gw + docs))


def test_ps20_valid_security_with_the_temporary_acme_responder_passes(gw):
    assert problems(gw + edge("--set", "acmeHttp01.enabled=true")) == []


# --- live side: the active RBAC filter and the CLI inputs -------------------------------------

def dump(policy_cidrs, action="DENY", filters=("local_ratelimit", "rbac", "basic_auth")):
    ids = [{"direct_remote_ip": {"address_prefix": c.split("/")[0], "prefix_len": int(c.split("/")[1])}} for c in policy_cidrs]
    http_filters = []
    for f in filters:
        entry = {"name": f"envoy.filters.http.{f}"}
        if f == "rbac":
            entry["typed_config"] = {"rules": {"action": action, "policies": {
                "ns[ewallet-ingress]-policy[edge-source-restriction]-rule[0]": {
                    "permissions": [{"any": True}], "principals": [{"and_ids": {"ids": [{"not_id": {"or_ids": {"ids": ids}}}]}}]}}}}
        http_filters.append(entry)
    return {"configs": [{"dynamic_listeners": [{"active_state": {"listener": {
        "address": {"socket_address": {"port_value": 443}},
        "filter_chains": [{"filters": [{"typed_config": {"http_filters": http_filters}}]}]}}}]}]}


def test_live_rbac_matches_the_approved_cidrs():
    approved = {ipaddress.ip_network(OWNER)}
    assert pf.rbac_problems(dump([OWNER]), approved, True) == []
    assert any("differ" in p for p in pf.rbac_problems(dump(["198.51.100.9/32"]), approved, True))
    assert any("no active DENY" in p for p in pf.rbac_problems(dump([OWNER], action="ALLOW"), approved, True))
    assert any("basic_auth" in p for p in pf.rbac_problems(dump([OWNER], filters=("local_ratelimit", "rbac")), approved, True))
    assert pf.rbac_problems(dump([], filters=("local_ratelimit", "rbac", "basic_auth")), approved, False) == []


def test_approved_cidrs_file_must_be_outside_the_repository(tmp_path):
    outside = tmp_path / "approved.txt"
    outside.write_text(f"# owner\n{OWNER}\n")
    args = lambda path: pf.argparse.Namespace(approved_cidr=[], approved_cidrs_file=str(path))
    assert pf.approved_cidrs(args(outside)) == ({ipaddress.ip_network(OWNER)}, [])
    inside = ROOT / "docs" / "approved.txt"
    networks, errors = pf.approved_cidrs(args(inside))
    assert networks is None and "inside the repository" in errors[0]
    bad = tmp_path / "bad.txt"
    bad.write_text("999.1.1.1/32\n")
    assert pf.approved_cidrs(args(bad))[0] is None


def test_cli_render_public_mode_end_to_end(tmp_path, gw, secure):
    manifests = tmp_path / "all.yaml"
    manifests.write_text(yaml.safe_dump_all(gw + secure))
    run = lambda *a: subprocess.run(["python3", str(ROOT / "scripts/edge/ownership_preflight.py"), "render", *a, str(manifests)],
                                    capture_output=True, text=True)
    ok = run("--allow-public-loadbalancer", "--domain", DOMAIN, "--approved-cidr", OWNER)
    assert ok.returncode == 0 and ok.stdout.strip().endswith("EDGE_OWNERSHIP_OK")
    assert run("--allow-public-loadbalancer", "--domain", DOMAIN).returncode == 1        # no approved CIDRs
    assert run("--allow-public-loadbalancer", "--approved-cidr", OWNER).returncode == 1  # no domain
