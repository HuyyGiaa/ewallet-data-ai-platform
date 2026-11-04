"""O1: matched one-factor configurations, shared-pool protection, selection rules."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/llm_agent"))
from ops.llm_agent import o1_guard as guard  # noqa: E402
import o1_benchmark as bench  # noqa: E402
import o1_compare as compare  # noqa: E402

CHART = ROOT / "infra/helm/llm-agent-platform"
HELM = shutil.which("helm")
IMAGE = "r.example/p/vllm-cpu-model@sha256:" + "a" * 64
CONFIGS = ("b0-prime", "c1-thread-bind", "c2-batched-tokens", "c3-no-prefix-cache")


def render(config):
    args = [HELM, "template", "x", str(CHART), "-f", str(CHART / "values-gke-cpu.yaml"),
            "-f", str(CHART / "values-gke-cpu-g1.yaml"), "-f", str(CHART / "o1/placement.yaml"),
            "-f", str(CHART / f"o1/{config}.yaml"), "--set", f"vllm.image={IMAGE}"]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return {d["metadata"]["name"]: d for d in yaml.safe_load_all(out) if d and d["kind"] == "Deployment"}


def serving(deployments):
    out = {}
    for name in ("vllm-primary", "vllm-fallback"):
        c = deployments[name]["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in c["env"]}
        out[name] = {"args": tuple(c["args"]), "env": env, "resources": c["resources"],
                     "replicas": deployments[name]["spec"]["replicas"], "image": c["image"]}
    return out


@unittest.skipUnless(HELM, "helm binary unavailable")
class MatchedConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renders = {c: serving(render(c)) for c in CONFIGS}

    def test_b0_prime_is_the_effective_g1_configuration(self):
        for pod in ("vllm-primary", "vllm-fallback"):
            spec = self.renders["b0-prime"][pod]
            self.assertIn("--enable-prefix-caching", spec["args"])   # G1 engine logs: True on both
            self.assertNotIn("--max-num-batched-tokens", spec["args"])
            self.assertEqual(spec["env"]["VLLM_CPU_OMP_THREADS_BIND"], "auto")
            self.assertEqual(spec["env"]["OMP_NUM_THREADS"], "2")
            self.assertEqual(spec["env"]["VLLM_CPU_KVCACHE_SPACE"], "2")
            self.assertEqual(spec["resources"], {"requests": {"cpu": "2", "memory": "6Gi"},
                                                 "limits": {"cpu": "3", "memory": "10Gi"}})
            self.assertEqual(spec["replicas"], 1)

    def test_every_candidate_changes_exactly_one_factor_and_no_resource(self):
        base = self.renders["b0-prime"]
        expected = {"c1-thread-bind": {"VLLM_CPU_OMP_THREADS_BIND"},
                    "c2-batched-tokens": {"--max-num-batched-tokens"},
                    "c3-no-prefix-cache": {"--enable-prefix-caching"}}
        for config, factor in expected.items():
            for pod in ("vllm-primary", "vllm-fallback"):
                with self.subTest(config=config, pod=pod):
                    cand = self.renders[config][pod]
                    self.assertEqual(cand["resources"], base[pod]["resources"])
                    self.assertEqual((cand["replicas"], cand["image"]), (base[pod]["replicas"], base[pod]["image"]))
                    env_diff = {k for k in set(cand["env"]) | set(base[pod]["env"])
                                if cand["env"].get(k) != base[pod]["env"].get(k)}
                    arg_diff = {a for a in set(cand["args"]) ^ set(base[pod]["args"]) if a.startswith("--")}
                    arg_diff = {"--enable-prefix-caching" if "prefix-caching" in a else a for a in arg_diff}
                    self.assertEqual(env_diff | arg_diff, factor)

    def test_placement_selects_o1_nodes_without_tolerations_or_priority(self):
        for name, d in render("b0-prime").items():
            spec = d["spec"]["template"]["spec"]
            self.assertEqual(spec["nodeSelector"], {"ewallet.io/o1-llm": "true"}, name)
            self.assertNotIn("tolerations", spec)
            self.assertNotIn("priorityClassName", spec)


def node(name, cpu="3920m", mem="13336472Ki", o1=False):
    labels = {"ewallet.io/o1-llm": "true"} if o1 else {}
    return {"metadata": {"name": name, "labels": labels}, "status": {"allocatable": {"cpu": cpu, "memory": mem},
            "conditions": [{"type": "MemoryPressure", "status": "False"}, {"type": "Ready", "status": "True"}]}}


def pod(ns, name, node_name, cpu="0", mem="0", ready=True, restarts=0):
    return {"metadata": {"namespace": ns, "name": name},
            "spec": {"nodeName": node_name, "containers": [{"resources": {"requests": {"cpu": cpu, "memory": mem}}}]},
            "status": {"phase": "Running", "containerStatuses": [{"ready": ready, "restartCount": restarts}]}}


VLLM_PAIR = [{"name": "vllm-primary", "cpu": 2.0, "memory_gib": 6.0},
             {"name": "vllm-fallback", "cpu": 2.0, "memory_gib": 6.0},
             {"name": "agentgateway", "cpu": 0.1, "memory_gib": 0.125}]


class CapacityTests(unittest.TestCase):
    def test_fits_on_the_two_o1_nodes_and_never_on_mlops_nodes(self):
        nodes = [node("mlops-a"), node("mlops-b"), node("o1-a", o1=True), node("o1-b", o1=True)]
        pods = [pod("kube-system", "ds-a", "o1-a", "1", "1.7Gi"), pod("kube-system", "ds-b", "o1-b", "1", "1.7Gi"),
                pod("kubeflow", "x", "mlops-a", "0.1", "1Gi")]
        result = guard.capacity(nodes, pods, VLLM_PAIR)
        self.assertEqual(result["verdict"], "FITS_SAFE_BUDGET")
        self.assertEqual({p["node"] for p in result["placement"]}, {"o1-a", "o1-b"})

    def test_fallback_counts_and_existing_requests_block(self):
        nodes = [node("mlops-a"), node("o1-a", o1=True)]                      # one O1 node only
        pods = [pod("kube-system", "ds", "o1-a", "1", "1.7Gi")]
        result = guard.capacity(nodes, pods, VLLM_PAIR)
        self.assertEqual(result["verdict"], "BLOCKED_INSUFFICIENT_SAFE_CAPACITY")
        self.assertEqual([p["name"] for p in result["unplaced"]], ["vllm-fallback"])

    def test_no_o1_nodes_is_blocked(self):
        self.assertEqual(guard.capacity([node("mlops-a")], [], VLLM_PAIR)["verdict"],
                         "BLOCKED_INSUFFICIENT_SAFE_CAPACITY")


class HealthAndGuardTests(unittest.TestCase):
    def snapshot(self, **pods):
        items = [pod("kserve", n, "mlops-a", ready=r, restarts=c) for n, (r, c) in pods.items()]
        isvc = [{"metadata": {"namespace": "fraud-serving", "name": "fraud-detector"},
                 "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]
        return guard.health(items, isvc, [node("mlops-a")])

    def test_regressions_are_new_problems_only(self):
        base = self.snapshot(a=(True, 0), b=(False, 3))                # b was already unhealthy
        self.assertEqual(guard.regressions(base, self.snapshot(a=(True, 0), b=(False, 3))), [])
        found = guard.regressions(base, self.snapshot(a=(False, 1), b=(False, 3)))
        self.assertTrue(any("not Ready" in f for f in found) and any("restarted" in f for f in found))

    def test_guard_thresholds(self):
        ok = {"nodes": [{"name": "m", "o1": False, "cpu_pct": 40.0, "mem_pct": 50.0},
                        {"name": "o", "o1": True, "cpu_pct": 99.0, "mem_pct": 80.0}], "vllm_mem_mib": 6000}
        self.assertEqual(guard.evaluate_guard(ok), [])      # O1 node CPU may be busy: it is O1's budget
        for change, word in (({"nodes": [{"name": "m", "o1": False, "cpu_pct": 90.0, "mem_pct": 50.0}]}, "CPU"),
                             ({"vllm_mem_mib": 9900}, "vLLM memory"),
                             ({"mlops_regressions": ["kserve/a not Ready"]}, "not Ready"),
                             ({"oom_or_eviction_events": ["t OOMKilling ewallet-agents/x"]}, "event")):
            sample = copy.deepcopy(ok)
            sample.update(change)
            self.assertTrue(any(word in r for r in guard.evaluate_guard(sample)), change)


class WorkloadTests(unittest.TestCase):
    def test_workload_is_deterministic_and_covers_every_request_type(self):
        a, b = bench.build_workload(), bench.build_workload()
        self.assertEqual(a["sha256"], b["sha256"])
        categories = {i["category"] for i in a["measured"]}
        self.assertEqual(categories, {"short_chat", "long_chat", "prefix_repeat", "prefix_unique", "tool_call",
                                      "nonstream_chat"})
        self.assertTrue(any(not i["stream"] for i in a["measured"]) and any(i["stream"] for i in a["measured"]))
        shared = {i["body"]["messages"][0]["content"] for i in a["measured"] if i["category"] == "prefix_repeat"}
        self.assertEqual(len(shared), 1)
        self.assertEqual(len({i["body"]["messages"][0]["content"] for i in a["cold_prefix"]}), 1)
        self.assertNotIn(next(iter(shared)), {i["body"]["messages"][0]["content"] for i in a["cold_prefix"]})

    def test_cli_bounds(self):
        script = ROOT / "scripts/llm_agent/o1_benchmark.py"
        base = ["--config", "x", "--resources", "r", "--stop-file", "s", "--output", "o"]
        for extra in (["--gateway", "http://10.0.0.1:3000", "--metrics", "http://127.0.0.1:1"],
                      ["--gateway", "http://127.0.0.1:1", "--metrics", "http://127.0.0.1:1", "--levels", "1,8"]):
            proc = subprocess.run([sys.executable, str(script), *base, *extra], capture_output=True, text=True,
                                  env={"PATH": "/usr/bin:/bin", "EWALLET_GATEWAY_KEY": "x"})
            self.assertEqual(proc.returncode, 2, proc.stderr)


def level(tps, ttft, p95, errors=0.0):
    return {"status": "MEASURED", "error_rate": errors, "timeout_rate": 0.0, "tool_calls_valid": 2,
            "output_tokens_per_second": tps, "ttft_ms": {"p50": ttft, "p95": ttft}, "itl_ms": {"p50": 1, "p95": 1},
            "tpot_ms": {"p50": 1, "p95": 1}, "e2e_ms": {"p50": p95, "p95": p95, "p99": p95},
            "requests_per_second": 0.1, "output_tokens_per_second_per_allocated_cpu": tps / 2,
            "server": {"queue_time_mean_ms": 0, "prefix_cache_hit_rate": 0.5}, "resources": None}


def summary(tps, ttft=1000, p95=10000, sha="w", errors=0.0):
    return {"workload_sha256": sha, "levels": {"1": level(tps, ttft, p95, errors), "4": level(tps, ttft, p95, errors)}}


class SelectionTests(unittest.TestCase):
    def run_compare(self, cands, windows=None):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for name, s in {"b0-prime": summary(3.5), **cands}.items():
                (folder / name).mkdir()
                (folder / name / "summary.json").write_text(json.dumps(s))
                (folder / name / "window.json").write_text(json.dumps((windows or {}).get(name, {})))
            compare.main([str(folder), "--configs", ",".join(["b0-prime", *cands])])
            return json.loads((folder / "comparison.json").read_text())

    def test_best_qualifying_candidate_is_selected(self):
        report = self.run_compare({"c1-thread-bind": summary(7.0, p95=6000), "c2-batched-tokens": summary(4.0)})
        self.assertEqual(report["selected"], "c1-thread-bind")
        # C2 (+14 %) also qualifies; C1 has the larger gain and is selected.
        self.assertTrue(report["candidates"]["c2-batched-tokens"]["qualifies"])

    def test_disqualifiers_lead_to_no_gain(self):
        cases = {"errors": ({"c1-thread-bind": summary(7.0, errors=0.1)}, None),
                 "mlops": ({"c1-thread-bind": summary(7.0)}, {"c1-thread-bind": {"mlops_regressions": ["x"]}}),
                 "workload": ({"c1-thread-bind": summary(7.0, sha="other")}, None),
                 "tail": ({"c1-thread-bind": summary(7.0, p95=20000)}, None),
                 "small": ({"c1-thread-bind": summary(3.7)}, None)}
        for label, (cands, windows) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.run_compare(cands, windows)["selected"], "NO_GAIN")


class GatewayBudgetTests(unittest.TestCase):
    """O1 run 3: B0-prime used ~20 000 of the 24 000 tokens/h gateway budget, then 429s."""

    def test_benchmark_budget_covers_every_config_and_production_default_is_unchanged(self):
        overlay = yaml.safe_load((CHART / "o1/benchmark-gateway.yaml").read_text())["agentgateway"]["tokenRate"]
        per_pass = 25_000                       # measured B0-prime pass (20 158) + cold/warm-up, rounded up
        needed = len(CONFIGS) * per_pass + 50_000  # + final smoke/failover gates
        self.assertEqual(overlay["interval"], "1h")
        self.assertGreaterEqual(overlay["capacity"], 1.5 * needed)
        self.assertEqual(overlay["capacity"], overlay["refill"])
        base = yaml.safe_load((CHART / "values.yaml").read_text())["agentgateway"]["tokenRate"]
        self.assertEqual(base["capacity"], 24000)   # G1/production guard unchanged

    def test_every_deploy_and_the_capacity_plan_use_the_same_overlay(self):
        text = (ROOT / "scripts/llm_agent/o1_experiment.sh").read_text()
        self.assertEqual(text.count("o1/benchmark-gateway.yaml"), 2)


class RunnerTests(unittest.TestCase):
    text = (ROOT / "scripts/llm_agent/o1_experiment.sh").read_text()

    def test_refuses_without_owner_approval(self):
        proc = subprocess.run([str(ROOT / "scripts/llm_agent/o1_experiment.sh"), tempfile.mkdtemp()],
                              capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "KUBE_CONTEXT": "c",
                                                                   "DIGESTS": "d"})
        self.assertEqual(proc.returncode, 2)
        self.assertIn("O1_APPROVED", proc.stderr)

    def test_port_forwards_background_kubectl_itself(self):
        # Through the k() wrapper, $! is a subshell: kill misses kubectl and the
        # stale forward keeps the port on deleted pods (O1 run 1, C1).
        for script in ("o1_experiment.sh", "g1_operator.sh"):
            text = (ROOT / "scripts/llm_agent" / script).read_text()
            self.assertNotRegex(text, r'\bk -n "\$NS" port-forward', script)

    def test_reaping_a_killed_forward_does_not_trip_errexit(self):
        # wait on a TERM-killed child returns 143; under set -e that ended O1 run 2.
        reaps = [line for line in self.text.splitlines() if 'wait "$pid"' in line]
        self.assertTrue(reaps)
        for line in reaps:
            self.assertIn("|| true", line)

    def test_cleanup_deletes_exactly_the_o1_instances_never_a_resize(self):
        cleanup = self.text[self.text.index("cleanup() {"):self.text.index("trap cleanup EXIT")]
        self.assertIn("delete-instances", cleanup)
        self.assertIn('-l "$LABEL=true"', cleanup)
        self.assertNotIn("resize", cleanup)
        self.assertIn("mlops-health-after.json", cleanup)


if __name__ == "__main__":
    unittest.main()
