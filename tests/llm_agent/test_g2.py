"""G2: B2 standard routing vs O2 llm-d routing over the same two C1 replicas.

Offline only: chart renders, the local preflight, guard/readback/benchmark/compare
logic on synthetic data, and the whole g2_experiment.sh run against a simulated
cluster (tests/llm_agent/g2_sim: fake kubectl/gcloud/helm, mock gateway and EPP),
including its failure, rate-limit, attribution and signal paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/llm_agent"))
from ops.llm_agent import g2_guard as guard  # noqa: E402
from ops.llm_agent import o1_guard  # noqa: E402
import g2_benchmark as bench  # noqa: E402
import g2_compare as compare  # noqa: E402
import g2_preflight as preflight  # noqa: E402
import g2_readback as readback  # noqa: E402
import o1_benchmark  # noqa: E402

CHART = ROOT / "infra/helm/llm-agent-platform"
HELM = shutil.which("helm")
IMAGE = "r.example/p/vllm-cpu-model@sha256:" + "a" * 64
SIM = ROOT / "tests/llm_agent/g2_sim"
ROUTER_CHART = os.environ.get("G2_ROUTER_CHART")  # llm-d-router-standalone-v1.0.0.tgz (digest-checked)
CONTEXT = "gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev"


def render(run, *extra):
    args = [HELM, "template", "llm-agent", str(CHART), "-f", str(CHART / "values-gke-cpu.yaml"),
            "-f", str(CHART / "values-gke-cpu-g1.yaml"), "-f", str(CHART / "g2/common.yaml"),
            "-f", str(CHART / "g2/benchmark-gateway.yaml"), "-f", str(CHART / f"g2/{run}.yaml"),
            "--set", f"vllm.image={IMAGE}", *extra]
    return subprocess.run(args, capture_output=True, text=True)


def router_render():
    out = subprocess.run([HELM, "template", "llmd-router", ROUTER_CHART, "-n", "ewallet-agents", "-f",
                          str(ROOT / "infra/helm/llm-d/standalone-values-g2.yaml")], capture_output=True, text=True,
                         check=True).stdout
    return preflight.docs(out)


O2_API = ("--set", "llmd.standalone.apiServer.cidrs={34.118.224.1/32}")


@unittest.skipUnless(HELM, "helm binary unavailable")
class ChartTests(unittest.TestCase):
    def test_b2_and_o2_run_identical_vllm_and_differ_only_in_routing(self):
        b2 = preflight.docs(render("b2").stdout)
        o2 = preflight.docs(render("o2", *O2_API).stdout)
        self.assertEqual(preflight.by_kind(b2, "Deployment")["vllm-primary"],
                         preflight.by_kind(o2, "Deployment")["vllm-primary"])
        routed = next(m for m in preflight.gateway_llm(o2)["models"] if m["name"] == "ewallet-fraud-llm-routed")
        self.assertEqual(routed["params"]["baseUrl"], "http://llmd-router-epp.ewallet-agents.svc.cluster.local:8081/v1")
        self.assertNotIn("Gateway", {d["kind"] for d in o2})            # standalone: no Gateway API object
        flows = json.loads(preflight.by_kind(o2, "ConfigMap")["llm-agent-network-flows"]["data"]["unresolved.json"])
        self.assertFalse(any(f["name"].startswith("agentgateway-to-llm-inference") for f in flows))

    def test_o2_refuses_without_api_server_readback(self):
        result = render("o2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("llmd.standalone.apiServer.cidrs", result.stderr)

    def test_gateway_mode_and_g1_render_unchanged(self):
        g1 = subprocess.run([HELM, "template", "x", str(CHART), "-f", str(CHART / "values-gke-cpu.yaml"),
                             "-f", str(CHART / "values-gke-cpu-g1.yaml"), "--set", f"vllm.image={IMAGE}"],
                            capture_output=True, text=True, check=True).stdout
        self.assertNotIn("llmd-router", g1)
        self.assertIn("VLLM_CPU_OMP_THREADS_BIND", g1)
        gw = subprocess.run([HELM, "template", "x", str(CHART), "--set", "llmd.enabled=true"],
                            capture_output=True, text=True, check=True).stdout
        self.assertIn("llm-d.ai/inference-pool: ewallet-fraud-llm", gw)   # gateway mode keeps the pool label
        self.assertIn("http://llm-inference-gateway.", gw)

    def test_token_budget_is_session_only_and_production_default_unchanged(self):
        values = yaml.safe_load((CHART / "values.yaml").read_text())
        self.assertEqual(values["agentgateway"]["tokenRate"]["capacity"], 24000)
        session = yaml.safe_load((CHART / "g2/benchmark-gateway.yaml").read_text())["agentgateway"]
        self.assertEqual(session["tokenRate"]["capacity"], 150000)
        # The failover gate (eviction, then two two-turn smokes on CPU) fits one eviction window.
        self.assertEqual((values["agentgateway"]["circuitBreakDuration"], session["circuitBreakDuration"]), ("30s", "300s"))
        o2 = yaml.safe_load(preflight.by_kind(preflight.docs(render("o2", *O2_API).stdout), "ConfigMap")
                            ["agentgateway-llm-config"]["data"]["config.yaml"])
        self.assertEqual({m["health"]["eviction"]["duration"] for m in o2["llm"]["models"]}, {"300s"})


@unittest.skipUnless(HELM and ROUTER_CHART, "helm or G2_ROUTER_CHART unavailable")
class PreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b2 = preflight.docs(render("b2").stdout)
        cls.o2 = preflight.docs(render("o2", *O2_API).stdout)
        cls.router = router_render()

    def test_passes_on_the_committed_configuration(self):
        result = preflight.check(self.b2, self.o2, self.router, [1, 2, 4, 8])
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["endpoint_selector"], "app.kubernetes.io/instance=primary,app.kubernetes.io/name=vllm")

    def test_catches_unfair_or_unsafe_changes(self):
        o2 = preflight.docs(render("o2", *O2_API, "--set", "vllm.primary.replicas=3").stdout)
        self.assertIn("vllm-primary differs between B2 and O2", preflight.check(self.b2, o2, self.router, [1])["problems"])
        b2 = preflight.docs(render("b2", "--set", "vllm.cpuThreadsBind=auto").stdout)
        o2 = preflight.docs(render("o2", *O2_API, "--set", "vllm.cpuThreadsBind=auto").stdout)
        self.assertTrue(any("VLLM_CPU_OMP_THREADS_BIND" in p for p in preflight.check(b2, o2, self.router, [1])["problems"]))
        open_router = [dict(d, data={"envoy.yaml": d["data"]["envoy.yaml"].replace(
            "failure_mode_allow: false", "failure_mode_allow: true")}) if d["kind"] == "ConfigMap"
            and "envoy.yaml" in d.get("data", {}) else d for d in self.router]
        self.assertTrue(any("fail closed" in p for p in preflight.check(self.b2, self.o2, open_router, [1])["problems"]))
        small = preflight.docs(render("b2", "--set", "agentgateway.tokenRate.capacity=24000").stdout)
        small_o2 = preflight.docs(render("o2", *O2_API, "--set", "agentgateway.tokenRate.capacity=24000").stdout)
        self.assertTrue(any("token budget" in p for p in preflight.check(small, small_o2, self.router, [1, 2, 4, 8])["problems"]))

    def test_router_render_has_no_cluster_scoped_objects(self):
        self.assertFalse([d for d in self.router if d["kind"].startswith("Cluster")])


class GuardTests(unittest.TestCase):
    def test_capacity_uses_only_g2_nodes(self):
        def node(name, labels):
            return {"metadata": {"name": name, "labels": labels},
                    "status": {"allocatable": {"cpu": "3920m", "memory": "13591636Ki"}}}
        nodes = [node("mlops", {}), node("old-o1", {"ewallet.io/o1-llm": "true"}),
                 *(node(f"g{i}", {"ewallet.io/g2-llm": "true"}) for i in range(3))]
        planned = [{"name": "vllm", "cpu": 2.0, "memory_gib": 6.0}] * 3 + [{"name": "gw", "cpu": 0.1, "memory_gib": 0.1}]
        result = guard.capacity(nodes, [], planned)
        self.assertEqual(result["verdict"], "FITS_SAFE_BUDGET")
        self.assertEqual(result["g2_nodes"], ["g0", "g1", "g2"])
        self.assertEqual(len({p["node"] for p in result["placement"] if p["name"] == "vllm"}), 3)
        self.assertEqual(guard.capacity(nodes, [], planned * 2)["verdict"], "BLOCKED_INSUFFICIENT_SAFE_CAPACITY")

    def test_pod_usage_is_per_pod_and_guard_limit_is_per_pod(self):
        rows = [["vllm-primary-a", "vllm", "2600m", "6200Mi"], ["vllm-primary-b", "vllm", "2500m", "6100Mi"],
                ["agentgateway-x", "agentgateway", "4m", "20Mi"], ["llmd-router-epp-1", "epp", "30m", "60Mi"],
                ["llmd-router-epp-1", "envoy-proxy", "20m", "40Mi"]]
        usage = guard.pod_usage(rows)
        self.assertEqual(usage["vllm_cpu_m"], 5100)
        self.assertEqual(usage["vllm_mem_mib"], 6200)    # largest pod, never the sum
        self.assertEqual(usage["router"]["envoy-proxy"]["cpu_m"], 20)
        self.assertEqual(o1_guard.evaluate_guard({"nodes": [], **usage}), [])


class ReadbackTests(unittest.TestCase):
    def test_records_serving_identity_and_fresh_pods(self):
        deploy = {"spec": {"replicas": 2, "template": {"spec": {"nodeSelector": {"ewallet.io/g2-llm": "true"},
                  "containers": [{"name": "vllm", "image": IMAGE, "args": ["--x"], "resources": {"r": 1},
                                  "env": [{"name": "A", "value": "1"},
                                          {"name": "K", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]}]}}}}
        def pod(name, node, started):
            return {"metadata": {"name": name}, "spec": {"nodeName": node},
                    "status": {"phase": "Running", "podIP": "10.0.0.1", "startTime": started,
                               "containerStatuses": [{"name": "vllm", "ready": True, "restartCount": 0, "imageID": "i@1"}]}}
        nodes = [{"metadata": {"name": n, "labels": {"node.kubernetes.io/instance-type": "e2-standard-4"}}} for n in "ab"]
        cm = {"data": {"config.yaml": yaml.safe_dump({"llm": {"policies": {"localRateLimit": [
            {"type": "tokens", "maxTokens": 1, "tokensPerFill": 1, "fillInterval": "1h"},
            {"type": "requests", "maxTokens": 12, "tokensPerFill": 12, "fillInterval": "1s"}]}}})}}
        out = readback.readback(deploy, [pod("p1", "a", "2026-11-01T10:00:05Z"), pod("p2", "b", "2026-11-01T10:00:09Z")],
                                nodes, cm, "2026-11-01T10:00:00Z")
        self.assertTrue(out["fresh_pods"] and out["distinct_nodes"])
        self.assertEqual(out["env"]["K"], "secret:s/k")
        self.assertEqual(out["node_class"], ["e2-standard-4"])
        stale = readback.readback(deploy, [pod("p1", "a", "2026-11-01T09:00:00Z"), pod("p2", "a", "2026-11-01T10:00:09Z")],
                                  nodes, cm, "2026-11-01T10:00:00Z")
        self.assertFalse(stale["fresh_pods"] or stale["distinct_nodes"])


METRICS = "vllm:num_requests_waiting 0\nvllm:kv_cache_usage_perc 0.1\nvllm:request_success_total{{m=\"x\"}} {n}\n" \
          "vllm:prompt_tokens_total {p}\nvllm:generation_tokens_total {g}\n"


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.counts = {"a": 0, "b": 0}
        self.turn = 0

    def scrapers(self, missing=False):
        def make(pod):
            def scrape():
                n = self.counts[pod]
                return "vllm:num_requests_running 0\n" if missing else METRICS.format(n=n, p=100 * n, g=10 * n)
            return scrape
        return {"a": make("a"), "b": make("b")}

    def request(self, backend="primary", status=200):
        def one(endpoint, key, item, *, timeout, level):
            self.turn += 1
            self.counts["a" if self.turn % 3 else "b"] += 1
            return {"id": item["id"], "category": item["category"], "level": level, "stream": True,
                    "status": status, "backend": backend, "error": None if status == 200 else f"HTTP Error {status}",
                    "tool_call_errors": [], "failed": status != 200, "timeout": False, "ttft_ms": 10.0,
                    "e2e_ms": 50.0, "itl_ms": [5.0], "tpot_ms": 5.0, "input_tokens": 10,
                    "output_tokens": 4 if status == 200 else 0, "started_at": 0.0}
        return one

    def run_bench(self, config="b2", **kw):
        argv = ["--config", config, "--gateway", "http://127.0.0.1:1", "--pods", "a,b", "--levels", "1,2",
                "--resources", str(self.tmp / "r.jsonl"), "--stop-file", str(self.tmp / "STOP"),
                "--output", str(self.tmp / config)]
        os.environ["EWALLET_GATEWAY_KEY"] = "k"
        code = bench.main(argv, scrapers=self.scrapers(kw.get("missing", False)),
                          request=self.request(kw.get("backend", "primary"), kw.get("status", 200)))
        return code, json.loads((self.tmp / config / "summary.json").read_text())

    def test_measures_levels_with_per_pod_distribution(self):
        code, summary = self.run_bench()
        self.assertEqual(code, 0)
        self.assertEqual(summary["workload_sha256"], o1_benchmark.build_workload()["sha256"])
        level = summary["levels"]["2"]
        self.assertEqual(level["status"], "MEASURED")
        per_pod = level["distribution"]["per_pod"]
        self.assertEqual(sum(d["vllm:request_success"] for d in per_pod.values()), 12)
        self.assertEqual(level["distribution"]["pods_serving"], 2)
        self.assertTrue(summary["vllm_signal_support"]["a"]["vllm:kv_cache_usage_perc"])
        self.assertFalse(summary["vllm_signal_support"]["a"]["vllm:lora_requests_info"])  # reported, never assumed

    def test_wrong_backend_is_not_comparable(self):
        code, summary = self.run_bench("o2", backend="primary")    # O2 served by the direct failover path
        self.assertEqual(code, 1)
        self.assertEqual(summary["levels"]["1"]["status"], "ATTRIBUTION_MISMATCH")

    def test_missing_metrics_block_before_any_request(self):
        code, summary = self.run_bench(missing=True)
        self.assertEqual((code, summary["status"]), (1, "BLOCKED_METRICS_MISSING"))
        self.assertEqual(self.turn, 0)

    def test_rate_limit_stops_and_is_reported(self):
        code, summary = self.run_bench(status=429)
        self.assertEqual(code, 1)
        self.assertEqual(summary["levels"]["1"]["status"], "BLOCKED_GATEWAY_RATE_LIMIT")
        self.assertNotIn("2", summary["levels"])

    def test_budget_and_level_bounds(self):
        b = bench.budget(o1_benchmark.build_workload(), [1, 2, 4, 8])
        self.assertGreater(b["pass_total"], 4 * b["per_level"])
        with self.assertRaises(SystemExit):
            bench.main(["--budget", "--levels", "16"])


def level(tps, ttft, e2e, err=0.0, share=None):
    return {"status": "MEASURED", "output_tokens_per_second": tps, "ttft_ms": {"p50": ttft, "p95": ttft * 2},
            "e2e_ms": {"p50": e2e, "p95": e2e}, "error_rate": err, "timeout_rate": 0.0,
            "distribution": {"pods_serving": 2, "requests_share": share or {"a": 0.5, "b": 0.5},
                             "requests_max_share": max((share or {"a": 0.5}).values()), "prompt_tokens_max_share": 0.5}}


class CompareTests(unittest.TestCase):
    def write(self, b2_levels, o2_levels, serving_o2=None):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        serving = {"image": IMAGE, "args": [], "env": {}, "resources": {}, "replicas": 2, "node_class": ["e2-standard-4"],
                   "token_rate": {"maxTokens": 150000}, "request_rate": {}, "pods": [{}, {}], "fresh_pods": True}
        for name, levels, srv in (("b2", b2_levels, serving), ("o2", o2_levels, serving_o2 or serving)):
            (d / name).mkdir()
            (d / name / "summary.json").write_text(json.dumps({"workload_sha256": "w", "status": "MEASURED", "levels": levels}))
            (d / name / "serving.json").write_text(json.dumps(srv))
        return d

    def test_gain_needs_a_loaded_level_and_no_regression(self):
        d = self.write({"4": level(10, 1000, 5000), "8": level(10, 2000, 9000)},
                       {"4": level(10.2, 990, 5000), "8": level(12, 1500, 8000)})
        self.assertEqual(compare.compare(d)["verdict"], "GAIN")

    def test_small_differences_are_no_gain_and_mixed_is_reported(self):
        d = self.write({"1": level(10, 1000, 5000), "4": level(10, 1000, 5000)},
                       {"1": level(10.3, 980, 5100), "4": level(10.4, 1030, 5200)})
        self.assertEqual(compare.compare(d)["verdict"], "NO_GAIN")
        d = self.write({"1": level(10, 1000, 5000), "8": level(10, 1000, 5000)},
                       {"1": level(10, 1300, 5000), "8": level(13, 900, 5000)})
        self.assertEqual(compare.compare(d)["verdict"], "MIXED")

    def test_a_baseline_error_is_compared_not_a_global_veto(self):
        # O1 erratum E1: one B2 failure must not disqualify the comparison.
        d = self.write({"1": level(10, 1000, 5000, err=1 / 12), "8": level(10, 1000, 5000)},
                       {"1": level(10, 1000, 5000), "8": level(12, 1000, 5000)})
        result = compare.compare(d)
        self.assertEqual(result["verdict"], "GAIN")
        self.assertEqual(result["fairness_issues"], [])

    def test_more_errors_in_o2_is_a_regression(self):
        d = self.write({"8": level(10, 1000, 5000)}, {"8": level(13, 800, 4000, err=1 / 12)})
        result = compare.compare(d)
        self.assertEqual(result["levels"]["8"]["regression"], ["errors"])
        self.assertEqual(result["verdict"], "NO_GAIN")

    def test_unfair_runs_are_inconclusive(self):
        srv = {"image": "other", "args": [], "env": {}, "resources": {}, "replicas": 2, "node_class": ["e2-standard-4"],
               "token_rate": {"maxTokens": 150000}, "request_rate": {}, "pods": [{}, {}], "fresh_pods": True}
        d = self.write({"8": level(10, 1000, 5000)}, {"8": level(20, 500, 2000)}, serving_o2=srv)
        result = compare.compare(d)
        self.assertEqual(result["verdict"], "INCONCLUSIVE")
        self.assertIn("serving image differs", result["fairness_issues"])

    def test_epp_decisions_are_counted_per_pod(self):
        # llm-d-router v1.0.0 logs the message under "body" (attempt 3 log); "msg" is accepted too.
        log = "\n".join([json.dumps({"body": "Request handled", "endpoint": "10.0.0.1:8000"})] * 3
                        + [json.dumps({"msg": "Request handled", "endpoint": "10.0.0.2:8000"}),
                           json.dumps({"body": "EPP received request"}), "not json"])
        # 10.0.0.1 must not also match 10.0.0.11: exact IP, never a substring.
        self.assertEqual(compare.epp_decisions(log, {"10.0.0.11": "x", "10.0.0.1": "a", "10.0.0.2": "b"}),
                         {"decisions": 4, "per_pod": {"a": 3, "b": 1}})

    def test_transient_scrape_failure_is_retried_once(self):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, "kubectl")
            return "ok"
        self.assertEqual(bench.retried(flaky, pause=0), "ok")
        with self.assertRaises(OSError):
            bench.retried(lambda: (_ for _ in ()).throw(OSError("down")), pause=0)

    def test_markdown_table_is_well_formed(self):
        d = self.write({"1": level(10, 1000, 5000)}, {"1": level(10, 1000, 5000)})
        header, delimiter = compare.table(d).splitlines()[:2]
        self.assertEqual(header.count("|"), delimiter.count("|"))


@unittest.skipUnless(HELM and ROUTER_CHART, "helm or G2_ROUTER_CHART unavailable")
class RunnerSimulationTests(unittest.TestCase):
    """g2_experiment.sh end to end against the simulated cluster (every stage, both runs, cleanup)."""

    def start(self, fail="", latency="0.02"):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        env = dict(os.environ, PATH=f"{SIM / 'bin'}:{os.environ['PATH']}", G2_SIM_STATE=str(d / "state.json"),
                   G2_SIM_REAL_HELM=HELM, G2_SIM_ROUTER_CHART=ROUTER_CHART, G2_SIM_FAIL=fail, G2_SIM_LATENCY=latency,
                   KUBE_CONTEXT=CONTEXT, DIGESTS=str(ROOT / "docs/evidence/llm_agent/o1_20261101T052939Z/image-digests.json"),
                   G2_APPROVED="yes", G2_ALLOW_DIRTY="yes", G2_OPERATOR=str(SIM / "fake_operator.sh"))
        proc = subprocess.Popen([str(ROOT / "scripts/llm_agent/g2_experiment.sh"), str(d / "ev")], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        return proc, d

    def finish(self, proc, d):
        out, _ = proc.communicate(timeout=600)
        state = json.loads((d / "state.json").read_text())
        cleanup = (d / "ev/cleanup.txt").read_text() if (d / "ev/cleanup.txt").exists() else ""
        return proc.returncode, out, state, cleanup

    def assert_clean(self, state, cleanup):
        self.assertEqual(sorted(state["nodes"]), ["sim-mlops-a", "sim-mlops-b"])
        self.assertEqual((state["pool"], state["releases"], state["pods"]), (2, [], {}))
        self.assertIn("pool_size=2", cleanup)
        self.assertFalse([c for c in state["calls"] if "clusters resize" in c and "--num-nodes 2" in c])
        leaked = subprocess.run(["pgrep", "-f", str(SIM / "mock_server.py")], capture_output=True, text=True).stdout
        self.assertEqual(leaked.strip(), "")

    def test_full_campaign(self):
        proc, d = self.start()
        code, out, state, cleanup = self.finish(proc, d)
        self.assertEqual(code, 0, out[-3000:])
        self.assert_clean(state, cleanup)
        result = json.loads((d / "ev/g2/comparison.json").read_text())
        self.assertEqual(result["fairness_issues"], [])
        self.assertIn(result["verdict"], ("GAIN", "NO_GAIN", "MIXED"))
        self.assertEqual(result["epp_decisions"]["decisions"], 53)        # cold 3 + warm-up 2 + 4 x 12
        for run in ("b2", "o2"):
            serving = json.loads((d / f"ev/g2/{run}/serving.json").read_text())
            self.assertTrue(serving["fresh_pods"] and serving["distinct_nodes"])
            self.assertEqual(json.loads((d / f"ev/g2/{run}/summary.json").read_text())["status"], "MEASURED")
        upgrades = [c.split()[3] for c in state["calls"] if c.startswith("helm upgrade")]
        # B2, then the O2 release with the router's allow policies, then the router itself.
        self.assertEqual(upgrades, ["llm-agent", "llm-agent", "llmd-router"])
        # The failover gate already replaced the primaries: no second sequential restart.
        self.assertFalse([c for c in state["calls"] if c.startswith("rollout restart deploy/vllm-primary")])
        self.assertIn("primaries fresh since", out)

    def test_gate_failure_stops_and_cleans_up(self):
        for fail in ("stage-failover", "deploy-llmd-router", "rollout"):
            with self.subTest(fail=fail):
                code, out, state, cleanup = self.finish(*self.start(fail))
                self.assertEqual(code, 1, out[-2000:])
                self.assertIn("FAIL", out)
                self.assertNotIn("benchmark o2", out)
                self.assert_clean(state, cleanup)

    def test_metrics_missing_stops_before_o2(self):
        proc, d = self.start("metrics-missing")
        code, out, state, cleanup = self.finish(proc, d)
        self.assertEqual(code, 1)
        self.assert_clean(state, cleanup)
        self.assertEqual(json.loads((d / "ev/g2/b2/summary.json").read_text())["status"], "BLOCKED_METRICS_MISSING")
        self.assertNotIn("gate D", out)                   # no O2 spend without a complete B2 control
        self.assertFalse((d / "ev/g2/comparison.json").exists())

    def test_o2_served_by_failover_is_recorded_inconclusive(self):
        proc, d = self.start("backend-o2")
        code, out, state, cleanup = self.finish(proc, d)
        self.assertEqual(code, 1)
        self.assert_clean(state, cleanup)
        comparison = json.loads((d / "ev/g2/comparison.json").read_text())
        self.assertEqual(comparison["verdict"], "INCONCLUSIVE")
        self.assertTrue(any("ATTRIBUTION_MISMATCH" in i for i in comparison["fairness_issues"]))

    def test_sigterm_during_benchmark_cleans_up_without_leaks(self):
        proc, d = self.start(latency="0.5")
        log = d / "ev/g2.log"
        deadline = time.time() + 300
        while time.time() < deadline and not (log.exists() and "benchmark b2" in log.read_text()):
            time.sleep(0.5)
        time.sleep(2)
        os.kill(proc.pid, signal.SIGTERM)
        started = time.time()
        code, out, state, cleanup = self.finish(proc, d)
        self.assertLess(time.time() - started, 60)       # the trap ran at once, not after the benchmark
        self.assertEqual(code, 130, out[-2000:])
        self.assertIn("interrupted", out)
        self.assert_clean(state, cleanup)


if __name__ == "__main__":
    unittest.main()
