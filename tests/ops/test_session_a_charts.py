"""Serving and edge charts: inert defaults, one scaler per target, real metric names."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVING = ROOT / "infra/helm/ewallet-serving"
EDGE = ROOT / "infra/helm/ewallet-edge"
SCHEMA = json.loads((ROOT / "infra/helm/keda/scaledobject-schema-2.21.0.json").read_text())
METRICS = set((ROOT / "tests/fixtures/serving_metric_names.txt").read_text().split())
IMAGE = "r.example/p/fraud-api@sha256:" + "a" * 64
# R2: the Feature API needs its own Workload Identity GSA.
GSA = ["--set", "featureApi.serviceAccount.gcpServiceAccount=ewallet-feature-api@p.iam.gserviceaccount.com"]
ON = GSA + ["--set", "featureApi.enabled=true", "--set", f"featureApi.image={IMAGE}", "--set", "featureApi.registryUri=gs://b/r",
      "--set", "metricsDiscovery.enabled=true", "--set", "autoscaling.enabled=true"]
ALL_ROUTES = ["feature-api", "kagent-ui", "agent-registry", "grafana"]
EDGE_ON = ["-n", "ewallet-ingress", "--set", "enabled=true", "--set", "domain=edge.example.test"] + [
    item for route in ALL_ROUTES for item in ("--set", f"routes.{route}.enabled=true")
]
EDGE_VERIFIED = EDGE_ON + ["--set", "securityFiltersVerified=true"]
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm unavailable")


def render(chart: Path, *args: str, check: bool = True):
    done = subprocess.run(["helm", "template", "t", str(chart), *args], capture_output=True, text=True)
    if not check:
        return done
    assert done.returncode == 0, done.stderr
    return [item for item in yaml.safe_load_all(done.stdout) if item]


def kinds(items, kind):
    return {item["metadata"]["name"]: item for item in items if item["kind"] == kind}


@pytest.mark.parametrize("chart", [SERVING, EDGE])
def test_nothing_renders_by_default(chart):
    assert render(chart) == []


def test_scaled_objects_match_the_installed_keda_schema():
    for item in kinds(render(SERVING, *ON), "ScaledObject").values():
        jsonschema.validate(item, SCHEMA["schema"])


def test_each_target_has_one_scaler_and_no_chart_hpa():
    items = render(SERVING, *ON)
    assert set(kinds(items, "ScaledObject")) == {"feature-api", "fraud-api"}
    assert not kinds(items, "HorizontalPodAutoscaler")
    assert "replicas" not in kinds(items, "Deployment")["feature-api"]["spec"]


def test_fixed_arm_sets_replicas_and_adds_no_scaler():
    items = render(SERVING, *GSA, "--set", "featureApi.enabled=true", "--set", f"featureApi.image={IMAGE}",
                   "--set", "featureApi.registryUri=gs://b/r")
    assert kinds(items, "Deployment")["feature-api"]["spec"]["replicas"] == 1
    assert not kinds(items, "ScaledObject")


def test_policy_is_one_to_three_with_a_safe_fallback():
    spec = kinds(render(SERVING, *ON), "ScaledObject")["fraud-api"]["spec"]
    assert (spec["minReplicaCount"], spec["maxReplicaCount"]) == (1, 3)
    assert spec["fallback"]["behavior"] == "currentReplicasIfHigher"


def test_rate_triggers_are_calibrated_from_the_g5a_baseline():
    objects = kinds(render(SERVING, *ON), "ScaledObject")
    expected = {"feature-api": ("60", "feature_api_requests_total"), "fraud-api": ("17", "fraud_inference_requests_total")}
    for name, (threshold, metric) in expected.items():
        [trigger] = objects[name]["spec"]["triggers"]  # rate only: no latency trigger by default
        assert (trigger["name"], trigger["metricType"]) == ("request-rate", "AverageValue")
        assert trigger["metadata"]["threshold"] == threshold
        query = trigger["metadata"]["query"]
        assert query.startswith(f"sum(rate({metric}{{job=\"{name}-pods\"}}[1m]))")


def test_queries_use_only_metrics_the_real_services_expose():
    for item in kinds(render(SERVING, *ON), "ScaledObject").values():
        for trigger in item["spec"]["triggers"]:
            used = set(re.findall(r"\b(?:feature_api|fraud_inference|backend_client)[a-z_]*", trigger["metadata"]["query"]))
            assert used and used <= METRICS, used - METRICS


def test_scaler_needs_per_pod_metrics():
    done = render(SERVING, "--set", "autoscaling.enabled=true", check=False)
    assert done.returncode != 0
    assert "metricsDiscovery" in done.stderr


def test_unpinned_image_is_refused():
    done = render(SERVING, "--set", "featureApi.enabled=true", "--set", "featureApi.image=fraud:latest", check=False)
    assert done.returncode != 0


def test_redis_is_never_a_scale_target():
    targets = {item["spec"]["scaleTargetRef"]["name"] for item in kinds(render(SERVING, *ON), "ScaledObject").values()}
    assert "redis" not in targets
    assert not any("predictor" in target for target in targets)


def test_feature_api_redis_access_is_additive_and_scoped():
    policy = kinds(render(SERVING, *ON), "NetworkPolicy")["redis-clients-feature-api"]
    assert policy["metadata"]["namespace"] == "feature-store"
    assert policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "feature-api"}


def test_edge_refuses_to_render_without_a_domain_or_a_route():
    assert render(EDGE, "-n", "ewallet-ingress", "--set", "enabled=true", check=False).returncode != 0
    assert render(EDGE, "-n", "ewallet-ingress", "--set", "enabled=true", "--set", "domain=edge.example.test",
                  check=False).returncode != 0


def test_edge_refuses_a_real_domain_until_the_security_filters_are_verified():
    real = [arg.replace("edge.example.test", "ewallet.example.com") for arg in EDGE_ON]
    assert render(EDGE, *real, check=False).returncode != 0
    assert render(EDGE, *real, "--set", "securityFiltersVerified=true", check=False).returncode == 0
    assert "EnvoyFilter" not in {item["kind"] for item in render(EDGE, *EDGE_ON)}


def test_edge_refuses_a_removed_or_incomplete_route():
    for route in ("fraud-api", "a2a"):
        assert render(EDGE, *EDGE_ON, "--set", f"routes.{route}.enabled=true", check=False).returncode != 0, route


def test_edge_refuses_a_namespace_other_than_its_own():
    assert render(EDGE, *EDGE_ON, "-n", "ewallet-edge", check=False).returncode != 0


def test_gateway_binds_to_the_helm_managed_service():
    # Without an address Istio provisions its own Deployment and a LoadBalancer Service.
    gateway = kinds(render(EDGE, *EDGE_ON), "Gateway")["ewallet-edge"]["spec"]
    assert gateway["gatewayClassName"] == "istio"
    assert gateway["addresses"] == [{"type": "Hostname", "value": "ewallet-edge-gateway.ewallet-ingress.svc.cluster.local"}]


def test_edge_serves_https_and_redirects_http():
    items = render(EDGE, *EDGE_ON)
    https, http = kinds(items, "Gateway")["ewallet-edge"]["spec"]["listeners"]
    assert https["protocol"] == "HTTPS" and https["port"] == 443 and https["tls"]["mode"] == "Terminate"
    assert http["protocol"] == "HTTP" and http["port"] == 80
    routes = kinds(items, "HTTPRoute")
    redirect = routes.pop("https-redirect")["spec"]
    assert redirect["parentRefs"] == [{"name": "ewallet-edge", "sectionName": "http"}]
    assert redirect["rules"][0]["filters"] == [{"type": "RequestRedirect", "requestRedirect": {"scheme": "https", "statusCode": 301}}]
    assert "backendRefs" not in redirect["rules"][0]
    assert {route["spec"]["parentRefs"][0]["sectionName"] for route in routes.values()} == {"https"}


def test_every_route_strips_identity_and_never_retries():
    routes = kinds(render(EDGE, *EDGE_ON), "HTTPRoute")
    routes.pop("https-redirect")
    assert set(routes) == set(ALL_ROUTES)
    for route in routes.values():
        rule = route["spec"]["rules"][0]
        assert rule["retry"] == {"attempts": 0}
        assert {"authorization", "x-user-id"} <= set(rule["filters"][0]["requestHeaderModifier"]["remove"])


def test_data_routes_expose_one_exact_method_and_path():
    rule = kinds(render(EDGE, *EDGE_ON), "HTTPRoute")["feature-api"]["spec"]["rules"][0]
    assert rule["matches"] == [{"path": {"type": "Exact", "value": "/v1/features/lookup"}, "method": "POST"}]


def test_internal_services_have_no_route():
    text = json.dumps(render(EDGE, *EDGE_ON))
    for name in ("vllm", "agentgateway", "mcp-gateway", "redis", "predictor", "mlflow", "jenkins", "vault",
                 "fraud-api", "fraud-coordinator", "kagent-controller"):
        assert name not in text, name


def test_each_reference_grant_admits_edge_routes_to_one_service():
    for grant in kinds(render(EDGE, *EDGE_ON), "ReferenceGrant").values():
        assert grant["spec"]["from"] == [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "namespace": "ewallet-ingress"}]
        assert len(grant["spec"]["to"]) == 1 and grant["spec"]["to"][0]["name"]


def test_rate_limiter_runs_before_basic_auth_and_every_route_has_a_limit():
    items = render(EDGE, *EDGE_VERIFIED)
    patches = kinds(items, "EnvoyFilter")["edge-security"]["spec"]["configPatches"]
    filters = [patch["patch"]["value"]["name"] for patch in patches if patch["applyTo"] == "HTTP_FILTER"]
    assert filters == ["envoy.filters.http.local_ratelimit", "envoy.filters.http.basic_auth"]
    limited = {patch["match"]["routeConfiguration"]["vhost"]["route"]["name"] for patch in patches if patch["applyTo"] == "HTTP_ROUTE"}
    # Istio names each Envoy route after its HTTPRoute rule; the patches match those names.
    rules = {route["spec"]["rules"][0]["name"] for name, route in kinds(items, "HTTPRoute").items() if name != "https-redirect"}
    assert limited == set(ALL_ROUTES) == rules


def test_security_filters_never_attach_to_the_redirect_listener():
    patches = kinds(render(EDGE, *EDGE_VERIFIED), "EnvoyFilter")["edge-security"]["spec"]["configPatches"]
    for patch in patches:
        if patch["applyTo"] != "HTTP_ROUTE":
            assert patch["match"]["listener"]["portNumber"] == 443


def test_no_credential_material_is_in_the_chart():
    for path in EDGE.rglob("*"):
        if path.is_file():
            assert "{SHA}" not in path.read_text() or path.name == "NOTES.txt"


def test_kagent_gets_no_allow_policy_that_would_deny_its_other_callers():
    assert not [p for p in kinds(render(EDGE, *EDGE_ON), "AuthorizationPolicy").values() if p["metadata"]["namespace"] == "kagent"]


def test_gateway_values_keep_the_service_private():
    values = yaml.safe_load((EDGE / "values-gateway.yaml").read_text())
    assert values["service"]["type"] == "ClusterIP"


def _preflight():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ownership_preflight", ROOT / "scripts/edge/ownership_preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ownership_preflight_rejects_the_legacy_gateway_profile():
    # values-gcp.yaml renders the llm-agent-platform Gateway without spec.addresses.
    legacy = render(ROOT / "infra/helm/llm-agent-platform", "-f", str(ROOT / "infra/helm/llm-agent-platform/values-gcp.yaml"))
    problems = _preflight().render_problems(legacy)
    assert any("ewallet-agents/ewallet-agent-gateway" in problem for problem in problems), problems


def test_ownership_preflight_accepts_the_edge_chart_alone():
    assert _preflight().render_problems(render(EDGE, *EDGE_ON)) == []


def test_ownership_preflight_detects_a_provisioned_duplicate_and_a_load_balancer():
    preflight = _preflight()
    meta = lambda ns, name, labels=None: {"namespace": ns, "name": name, "labels": labels or {}}
    edge = {"metadata": meta("ewallet-ingress", "ewallet-edge"), "spec": {"gatewayClassName": "istio",
            "addresses": [{"type": "Hostname", "value": "ewallet-edge-gateway.ewallet-ingress.svc.cluster.local"}]}}
    waypoint = {"metadata": meta("ewallet-agents", "waypoint"), "spec": {"gatewayClassName": "istio-waypoint"}}
    edge_service = {"metadata": meta("ewallet-ingress", "ewallet-edge-gateway", {"app.kubernetes.io/managed-by": "Helm"}),
                    "spec": {"type": "ClusterIP"}}
    assert preflight.live_problems([edge, waypoint], [], [edge_service]) == []
    legacy = {"metadata": meta("ewallet-agents", "ewallet-agent-gateway"), "spec": {"gatewayClassName": "istio"}}
    provisioned = {"metadata": meta("ewallet-agents", "ewallet-agent-gateway-istio",
                                    {"gateway.istio.io/managed": "istio.io-gateway-controller"})}
    balancer = {"metadata": meta("ewallet-agents", "ewallet-agent-gateway-istio"), "spec": {"type": "LoadBalancer"}}
    problems = preflight.live_problems([edge, legacy], [provisioned], [edge_service, balancer])
    assert len(problems) == 4, problems  # unexpected Gateway, unbound Gateway, provisioned Deployment, LoadBalancer
    assert len(preflight.live_problems([edge], [], [edge_service, balancer], frozenset({"ewallet-agents/ewallet-agent-gateway-istio"}))) == 0
