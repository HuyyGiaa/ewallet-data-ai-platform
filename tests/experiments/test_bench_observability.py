"""Unit tests for the benchmark watchdog, observability, cost plan, policy probe and checkpoint."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from ops.experiments import bench_dashboard, bench_watchdog, checkpoint, cost_plan, gateway_policy, pod_usage_exporter
from ops.experiments import prom_readback

ROOT = Path(__file__).resolve().parents[2]
RENDER = ROOT / "scripts/experiments/render_bench.py"
HELPER = ("asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/g3-byo-coordinator@sha256:"
          + "4" * 64)
NOW = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)
# Metric names read from real scrapes of the pinned versions (vLLM 0.32 CPU and agentgateway
# v1.7.0 through production Prometheus; llm-d EPP v1.0.0 /metrics from G2).
VLLM_NAMES = ROOT / "docs/evidence/llm_agent/edai2_integration_20261102/observability/prometheus-metric-names.json"
EPP_SCRAPE = ROOT / "docs/evidence/llm_agent/g2_20261101T124444Z/g2/o2/epp-metrics-after.txt"


# --- watchdog -------------------------------------------------------------------------

class FakeApi:
    def __init__(self, items: dict[str, list[dict]], fail_delete: str = "") -> None:
        self.items, self.fail_delete, self.calls = items, fail_delete, []

    def __call__(self, method: str, path: str):
        self.calls.append((method, path))
        resource = path.rstrip("/").split("/")[-1] if method == "GET" else path.split("/")[-2]
        if method == "GET":
            return 200, {"items": self.items.get(resource, [])}
        path = path.removesuffix("?orphan")
        name = path.split("/")[-1]
        if name == self.fail_delete:
            return 403, {}
        self.items[resource] = [i for i in self.items.get(resource, []) if i["metadata"]["name"] != name]
        return 200, {}


def env(deadline: datetime | str, mode: str = "enforce") -> dict[str, str]:
    value = deadline if isinstance(deadline, str) else deadline.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"NAMESPACE": "ewallet-bench", "DEADLINE_UTC": value, "MODE": mode, "CRONJOB_NAME": "bench-watchdog"}


def test_watchdog_idles_before_the_deadline() -> None:
    api = FakeApi({"deployments": [{"metadata": {"name": "vllm-primary"}}]})
    code, report = bench_watchdog.run(env(NOW + timedelta(minutes=5)), api, NOW)
    assert code == 0 and report["event"] == "watchdog.idle" and report["seconds_left"] == 300
    assert api.calls == []


def test_watchdog_deletes_workloads_then_itself_but_not_its_own_job() -> None:
    own = {"metadata": {"name": "bench-watchdog-29", "ownerReferences": [{"kind": "CronJob", "name": "bench-watchdog"}]}}
    api = FakeApi({"deployments": [{"metadata": {"name": "vllm-primary"}}, {"metadata": {"name": "bench-grafana"}}],
                   "statefulsets": [], "jobs": [own, {"metadata": {"name": "other"}}]})
    code, report = bench_watchdog.run(env(NOW - timedelta(seconds=1)), api, NOW)
    assert code == 0 and report["event"] == "watchdog.enforced"
    assert report["deleted"] == ["deployments/vllm-primary", "deployments/bench-grafana", "jobs/other",
                                 "cronjobs/bench-watchdog"]
    deletes = [path for method, path in api.calls if method == "DELETE"]
    assert deletes[-1].endswith("/cronjobs/bench-watchdog?orphan")
    assert not any(path.endswith("?orphan") for path in deletes[:-1])
    assert all("/namespaces/ewallet-bench/" in path for _, path in api.calls)
    assert report["remaining"] == []


def test_watchdog_reports_a_failed_delete() -> None:
    api = FakeApi({"deployments": [{"metadata": {"name": "vllm-primary"}}]}, fail_delete="vllm-primary")
    code, report = bench_watchdog.run(env(NOW), api, NOW)
    assert code == 1 and report["errors"] == ["delete deployments/vllm-primary: 403"]
    assert report["remaining"] == ["deployments/vllm-primary"]


def test_watchdog_without_a_readable_deadline_enforces_rather_than_idles() -> None:
    api = FakeApi({"deployments": [{"metadata": {"name": "vllm-primary"}}]})
    code, report = bench_watchdog.run(env(""), api, NOW)
    assert report["event"] == "watchdog.enforced" and "enforcing" in report["reason"] and code == 0


@pytest.mark.parametrize(("environ", "event"), [(env(NOW, "dry-run"), "watchdog.would_enforce"),
                                                (env(NOW, "maybe"), "watchdog.misconfigured"),
                                                (env("2026-11-09T03:00:00", "dry-run"), "watchdog.misconfigured")])
def test_watchdog_modes(environ, event) -> None:
    api = FakeApi({})
    _, report = bench_watchdog.run(environ, api, NOW)
    assert report["event"] == event and not [c for c in api.calls if c[0] == "DELETE"]


# --- exporter -------------------------------------------------------------------------

def test_exporter_converts_quantities_and_labels_per_container() -> None:
    listing = {"items": [{"metadata": {"name": "vllm-primary-a"}, "window": "15.2s", "containers": [
        {"name": "vllm", "usage": {"cpu": "1500000000n", "memory": "6291456Ki"}}]}]}
    text = pod_usage_exporter.render(listing, 0, 1.0)
    assert 'bench_pod_container_cpu_cores{pod="vllm-primary-a",container="vllm"} 1.5' in text
    assert f'bench_pod_container_memory_working_set_bytes{{pod="vllm-primary-a",container="vllm"}} {6 * 1024 ** 3}' in text
    assert pod_usage_exporter.cpu_cores("250m") == 0.25 and pod_usage_exporter.memory_bytes("1Gi") == 1024 ** 3


def test_exporter_failure_shows_as_missing_data_not_stale_values() -> None:
    answers = [{"items": [{"metadata": {"name": "p"}, "containers": [{"name": "c", "usage": {"cpu": "1"}}]}]}]

    def fetch():
        if answers:
            return answers.pop()
        raise OSError("api down")

    poller = pod_usage_exporter.Poller(fetch)
    poller.poll()
    assert "bench_pod_container_cpu_cores" in poller.text().split("# TYPE bench_pod_usage_window_seconds gauge")[1]
    poller.poll()
    text = poller.text()
    assert 'pod="p"' not in text and "bench_pod_usage_poll_errors_total 1" in text


# --- cost plan ------------------------------------------------------------------------

@pytest.mark.parametrize("nodes", [1, 2])
def test_default_sessions_fit_the_owner_ceilings(nodes) -> None:
    plan = cost_plan.plan(nodes, 210, max_usd=5, max_window_minutes=240)
    assert plan["fits"] and plan["session_window_minutes"] == 235 and plan["worst_case_usd"] < 2


def test_cost_plan_refuses_a_third_node_and_long_windows() -> None:
    assert cost_plan.plan(3, 60, max_usd=5, max_window_minutes=240)["problems"][0].startswith("3 nodes")
    assert not cost_plan.plan(2, 230, max_usd=5, max_window_minutes=240)["fits"]
    assert not cost_plan.plan(2, 210, max_usd=0.5, max_window_minutes=240)["fits"]


# --- dashboard and readback -----------------------------------------------------------

def known_metric_names() -> set[str]:
    body = json.loads(VLLM_NAMES.read_text(encoding="utf-8"))
    names = set(body["data"] if isinstance(body, dict) else body)
    names |= {line.split("{")[0].split(" ")[0] for line in EPP_SCRAPE.read_text(encoding="utf-8").splitlines()
              if line and not line.startswith("#")}
    exporter = pod_usage_exporter.render({"items": [{"metadata": {"name": "p"}, "containers": [
        {"name": "c", "usage": {"cpu": "1", "memory": "1Mi"}}]}]}, 0, 1.0)
    names |= {line.split("{")[0].split(" ")[0] for line in exporter.splitlines() if line and not line.startswith("#")}
    return names | {"up"}


def test_every_dashboard_metric_exists_in_a_real_scrape() -> None:
    known = known_metric_names()
    used = set()
    for item in bench_dashboard.queries():
        for name in re.findall(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\{", item["expr"]):
            used.add(name)
        assert "or vector(0)" not in item["expr"], "a missing metric must not render as zero"
    missing = sorted(used - known)
    assert not missing, missing


def test_dashboard_covers_the_required_panel_groups() -> None:
    titles = " | ".join(title for _, title, *_ in bench_dashboard.PANELS).lower()
    for word in ("output tokens", "ttft", "end-to-end", "running and waiting", "cpu", "memory", "queue time",
                 "prefix-cache", "per replica", "ready endpoints", "scheduling decisions", "errors", "scrape health"):
        assert word in titles, word
    model = bench_dashboard.dashboard()
    assert model["uid"] == bench_dashboard.UID and model["editable"] is False
    assert all(p.get("fieldConfig", {}).get("defaults", {}).get("noValue") == bench_dashboard.NO_VALUE
               for p in model["panels"] if p["type"] not in ("row", "text"))


@pytest.mark.parametrize(("run_id", "expected"), [("baseline-01", "baseline-01"), ("a.b", "a\\\\.b"), ("", ".*")])
def test_readback_fills_variables_like_grafana(run_id, expected) -> None:
    expr = prom_readback.substitute('x{run_id=~"$run_id"}[$__range]', run_id, 30)
    assert expr == f'x{{run_id=~"{expected}"}}[60s]'


def test_readback_reports_data_empty_and_errors_per_panel() -> None:
    def fetch(url: str):
        if "llm_d_epp" in url:
            return {"data": {"result": []}}
        if "agentgateway" in url:
            raise OSError("connection refused")
        if "targets" in url:
            return {"data": {"activeTargets": [{"labels": {"job": "vllm", "pod": "vllm-primary-a"}, "health": "up"},
                                               {"labels": {"job": "vllm", "pod": "vllm-primary-b"}, "health": "up"}]}}
        return {"data": {"result": [{"metric": {"pod": "vllm-primary-a", "run_id": "optimized-01"},
                                     "values": [[1, "1"], [2, "2"]]}]}}

    report = prom_readback.readback(fetch, "http://127.0.0.1:1", start="2026-11-09T01:00:00Z",
                                    end="2026-11-09T01:10:00Z", run_id="optimized-01")
    status = {p["panel"]: p["status"] for p in report["panels"]}
    assert status["Output tokens/s per vLLM pod"] == "DATA"
    assert status["llm-d EPP ready endpoints"] == "EMPTY"
    assert status["Gateway responses by HTTP status"] == "QUERY_ERROR"
    assert [t["pod"] for t in report["targets"]] == ["vllm-primary-a", "vllm-primary-b"]
    share = next(p for p in report["panels"] if p["panel"].startswith("Share"))
    assert share["mode"] == "instant" and "[600s]" in share["query"]


# --- gateway policy -------------------------------------------------------------------

def fake_gateway(client: str, backend: str, *, limit: int = 12, header: str = "primary", reject_missing: bool = True):
    state = {"burst": 0}

    def send(key):
        if key is None:
            return {"status": 401 if reject_missing else 200, "headers": {}}
        if key != client:
            return {"status": 401, "headers": {}, "body": "unauthorized"}
        state["burst"] += 1
        if state["burst"] > 4 + limit:
            return {"status": 429, "headers": {"retry-after": "1"}, "body": "rate limited"}
        return {"status": 200, "headers": {"x-ewallet-backend": header}}

    return send


def test_policy_probe_passes_on_a_correct_gateway_and_never_records_keys() -> None:
    report = gateway_policy.probe(fake_gateway("c" * 64, "b" * 64), client_key="c" * 64, backend_key="b" * 64,
                                  expect_backend="primary", burst=16, epp=None)
    assert report["verdict"] == "PASS"
    burst = next(c for c in report["cases"] if c["case"] == "rate_limit_burst")
    assert burst["status_counts"] == {"200": 12, "429": 4}
    assert "c" * 64 not in json.dumps(report) and "b" * 64 not in json.dumps(report)


@pytest.mark.parametrize("kwargs", [{"reject_missing": False}, {"limit": 100}, {"header": "fallback"}])
def test_policy_probe_fails_when_a_policy_is_not_enforced(kwargs) -> None:
    report = gateway_policy.probe(fake_gateway("c", "b", **kwargs), client_key="c", backend_key="b",
                                  expect_backend="primary", burst=16, epp=None)
    assert report["verdict"] == "FAIL"


def test_policy_probe_needs_the_epp_counter_to_grow_in_the_llm_d_arm() -> None:
    counts = iter(["llm_d_epp_request_total{a=\"b\"} 10\n", "llm_d_epp_request_total{a=\"b\"} 11\n"])
    report = gateway_policy.probe(fake_gateway("c", "b", header="llm-d"), client_key="c", backend_key="b",
                                  expect_backend="llm-d", burst=16, epp=lambda: next(counts))
    routing = next(c for c in report["cases"] if c["case"] == "routing_backend_header")
    assert routing["epp_request_total_delta"] == 1 and routing["passed"] is False


def test_policy_probe_without_backend_key_marks_that_case_not_executed() -> None:
    report = gateway_policy.probe(fake_gateway("c", "b"), client_key="c", backend_key=None,
                                  expect_backend="primary", burst=16, epp=None)
    case = next(c for c in report["cases"] if c["case"] == "backend_key_as_client_key")
    assert case["status"] == "NOT_EXECUTED" and report["verdict"] == "PASS"


# --- checkpoint -----------------------------------------------------------------------

def test_checkpoint_banner_links_the_run_window(tmp_path) -> None:
    (tmp_path / "summary.json").write_text(json.dumps({"workload_version": "o1-workload-v1", "workload_sha256": "abc",
        "levels": {"4": {"status": "MEASURED", "output_tokens_per_second": 21.5, "ttft_ms": {"p50": 900.0},
                         "e2e_ms": {"p95": 12000.0}, "error_rate": 0.0, "requests": 12}}}))
    (tmp_path / "controls.json").write_text(json.dumps({"thread_bind": "0-1", "replicas": 1, "model_revision": "c18",
                                                         "image_digest": "sha256:f695", "node_names": "n1"}))
    args = argparse.Namespace(title="EXP-V1 / OPTIMIZED run 01", experiment="EXP-V1", arm="optimized",
                              label="c1-thread-bind", route="Service -> vLLM", run_id="optimized-01",
                              kube_context="ctx", grafana="http://127.0.0.1:13030", prometheus="http://127.0.0.1:13090",
                              window_start="2026-11-09T01:00:00Z", window_end="2026-11-09T01:10:00Z", wait_minutes="20",
                              run_dir=tmp_path, summary=tmp_path / "summary.json", controls=tmp_path / "controls.json",
                              readback=None, policy=None, screenshots=tmp_path / "shots")
    text = checkpoint.banner(args)
    assert "OWNER EVIDENCE CHECKPOINT" in text and "Thread binding: 0-1" in text
    assert "c=4  out tok/s 21.5" in text and "var-run_id=optimized-01" in text
    assert f"&from={int(datetime(2026, 11, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)}" in text
    assert "Type CONTINUE" in text and "llmd-router-epp" not in text


# --- rendered manifests ---------------------------------------------------------------

def render(*args: str) -> list[dict]:
    out = subprocess.run([sys.executable, str(RENDER), "exp-v2", "baseline", *args, "--helper-image", HELPER,
                          "--api-server-cidr", "10.20.0.8/32", "--api-server-port", "443"],
                         capture_output=True, text=True, check=True).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


@pytest.mark.parametrize("stack", [("--stack", "observability"),
                                   ("--stack", "watchdog", "--deadline-utc", "2026-11-09T04:00:00Z")])
def test_extra_stacks_stay_inside_the_bench_namespace_and_pool(stack) -> None:
    docs = render(*stack)
    assert {"ClusterRole", "ClusterRoleBinding", "Ingress", "Namespace"}.isdisjoint(d["kind"] for d in docs)
    assert all(d["metadata"]["namespace"] == "ewallet-bench" for d in docs)
    for doc in docs:
        if doc["kind"] == "Service":
            assert doc["spec"].get("type", "ClusterIP") == "ClusterIP"
        pod = (doc.get("spec", {}).get("template", {}) or doc.get("spec", {}).get("jobTemplate", {}).get("spec", {})
               .get("template", {})).get("spec")
        if pod:
            assert pod["nodeSelector"] == {"ewallet.io/bench": "true"}
            assert all(re.search(r"@sha256:[0-9a-f]{64}$", c["image"]) for c in pod["containers"])
        if doc["kind"] == "Role":
            for rule in doc["rules"]:
                assert "secrets" not in rule["resources"] and "*" not in rule["verbs"]


def test_prometheus_discovers_only_the_bench_namespace_one_target_per_pod() -> None:
    config = next(d for d in render("--stack", "observability") if d["metadata"]["name"] == "bench-prometheus-config")
    prom = yaml.safe_load(config["data"]["prometheus.yaml"])
    for job in prom["scrape_configs"]:
        for sd in job.get("kubernetes_sd_configs", []):
            assert sd == {"role": "pod", "namespaces": {"names": ["ewallet-bench"]}}
            keep = job["relabel_configs"][0]
            assert keep["action"] == "keep" and keep["regex"].startswith("Running;")
    assert [j["job_name"] for j in prom["scrape_configs"]] == ["vllm", "agentgateway", "epp", "pod-usage"]
    grafana = next(d for d in render("--stack", "observability") if d["metadata"]["name"] == "bench-grafana"
                   and d["kind"] == "Deployment")
    values = {e["name"]: e.get("value") for e in grafana["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert values["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Viewer" and values["GF_SECURITY_ADMIN_PASSWORD"] is None


def test_watchdog_role_can_only_delete_workloads_and_its_own_cronjob() -> None:
    role = next(d for d in render("--stack", "watchdog", "--deadline-utc", "2026-11-09T04:00:00Z") if d["kind"] == "Role")
    assert role["rules"] == [
        {"apiGroups": ["apps"], "resources": ["deployments", "statefulsets"], "verbs": ["list", "delete"]},
        {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["list", "delete"]},
        {"apiGroups": ["batch"], "resources": ["cronjobs"], "resourceNames": ["bench-watchdog"], "verbs": ["delete"]}]


def test_watchdog_needs_an_explicit_utc_deadline() -> None:
    result = subprocess.run([sys.executable, str(RENDER), "exp-v1", "baseline", "--stack", "watchdog", "--helper-image",
                             HELPER, "--deadline-utc", "tomorrow", "--api-server-cidr", "1.2.3.4/32",
                             "--api-server-port", "443"], capture_output=True, text=True, check=False)
    assert result.returncode != 0 and "deadline-utc" in result.stderr


def test_api_server_egress_lists_endpoint_and_service_address() -> None:
    out = subprocess.run([sys.executable, str(RENDER), "exp-v2", "baseline", "--stack", "observability",
                          "--helper-image", HELPER, "--api-server-cidr", "10.20.0.8/32,34.118.224.1/32",
                          "--api-server-port", "443"], capture_output=True, text=True, check=True).stdout
    policy = next(d for d in yaml.safe_load_all(out) if d and d["kind"] == "NetworkPolicy")
    assert policy["spec"]["egress"] == [{"to": [{"ipBlock": {"cidr": "10.20.0.8/32"}},
                                               {"ipBlock": {"cidr": "34.118.224.1/32"}}],
                                         "ports": [{"protocol": "TCP", "port": 443}]}]
    bad = subprocess.run([sys.executable, str(RENDER), "exp-v2", "baseline", "--stack", "observability",
                          "--helper-image", HELPER, "--api-server-cidr", "10.0.0.0/8", "--api-server-port", "443"],
                         capture_output=True, text=True, check=False)
    assert bad.returncode != 0 and "/32" in bad.stderr
