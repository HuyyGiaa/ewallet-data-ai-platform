"""R1 (ModelConfig -> agentgateway -> CPU vLLM) offline readiness.

Covers the chart contract (routing, model identity, nested timeouts, no
replay of generations), the acceptance client (tool_calls validation, SSE
assembly), the read-only preflight, and the committed OFFLINE mock evidence:
the real agentgateway v1.7.0 binary in front of a mock vLLM (not a model).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from ops.llm_agent import r1_preflight

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
HELM = shutil.which("helm")
EVIDENCE = ROOT / "docs/evidence/llm_agent/source_review/r1_prep/gateway_mock_scenarios.json"
_spec = importlib.util.spec_from_file_location("smoke_tool_call", ROOT / "scripts/llm_agent/smoke_tool_call.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


def render(*files, overrides=None, check=True):
    with tempfile.TemporaryDirectory() as folder:
        args = [HELM, "template", "llm-agent", str(CHART)]
        for name in files:
            args += ["-f", str(CHART / name)]
        if overrides:
            path = Path(folder) / "override.yaml"
            path.write_text(yaml.safe_dump(overrides))
            args += ["-f", str(path)]
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if not check:
        return proc
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def find(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def seconds(value):
    return float(value[:-1]) if value.endswith("s") else float(value)


R1 = ("values-local.yaml", "values-r1-local.yaml")


@unittest.skipUnless(HELM, "helm binary unavailable")
class R1ChartContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render(*R1)
        cls.config = yaml.safe_load(find(cls.docs, "ConfigMap", "agentgateway-llm-config")["data"]["config.yaml"])

    def test_modelconfig_reaches_the_vllm_served_model_through_the_gateway(self):
        model_config = find(self.docs, "ModelConfig", "ewallet-global-llm")["spec"]
        service = find(self.docs, "Service", "agentgateway")["spec"]["ports"][0]
        self.assertEqual(model_config["openAI"]["baseUrl"], "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1")
        self.assertEqual((service["port"], service["targetPort"]), (3000, "entry"))
        self.assertEqual(self.config["gateways"]["default"]["port"], 4000)
        self.assertEqual(self.config["routes"][0]["backends"], [{"host": "127.0.0.1:3000"}])
        self.assertEqual(self.config["llm"]["port"], 3000)
        self.assertIn(model_config["model"], {v["name"] for v in self.config["llm"]["virtualModels"]})
        args = find(self.docs, "Deployment", "vllm-primary")["spec"]["template"]["spec"]["containers"][0]["args"]
        served = args[args.index("--served-model-name") + 1]
        for model in self.config["llm"]["models"]:
            self.assertEqual(model["params"]["model"], served)
            self.assertEqual(model["provider"]["custom"]["formats"], [{"type": "completions"}])
        self.assertEqual(self.config["llm"]["models"][0]["params"]["baseUrl"],
                         "http://vllm-primary.ewallet-agents.svc.cluster.local:8000/v1")

    def test_budgets_nest_from_client_to_backend(self):
        client = float(find(self.docs, "ModelConfig", "ewallet-global-llm")["spec"]["openAI"]["timeout"])
        entry = seconds(self.config["routes"][0]["policies"]["timeout"]["requestTimeout"])
        listener = self.config["llm"]["policies"]["timeout"]
        self.assertGreater(client, entry)
        self.assertGreater(entry, seconds(listener["requestTimeout"]))
        self.assertGreater(seconds(listener["requestTimeout"]), seconds(listener["backendRequestTimeout"]))
        self.assertGreaterEqual(seconds(listener["backendRequestTimeout"]), seconds(listener["responseIdleTimeout"]))
        # The acceptance client waits longer than the gateway's own budget.
        self.assertGreater(220.0, entry)

    def test_generations_are_never_replayed(self):
        entry = self.config["routes"][0]["policies"]
        self.assertNotIn("backendRequestTimeout", entry["timeout"])
        self.assertEqual(entry["retry"]["codes"], [502, 503])
        self.assertEqual(entry["retry"]["attempts"], 1)
        self.assertNotIn("retry", self.config["llm"]["policies"])

    def test_guards_reject_unsafe_budgets_and_retries(self):
        cases = {
            "client not above entry": {"modelConfig": {"timeoutSeconds": 180}},
            "listener above entry": {"agentgateway": {"llmRequestTimeout": "190s"}},
            "replay on 504": {"agentgateway": {"retryCodes": [502, 504]}},
            "two retries": {"agentgateway": {"retryAttempts": 2}},
        }
        for label, override in cases.items():
            with self.subTest(label):
                self.assertNotEqual(render(*R1, overrides=override, check=False).returncode, 0)

    def test_r1_scope_is_minimal(self):
        kinds = {(d["kind"], d["metadata"]["name"]) for d in self.docs}
        self.assertNotIn(("Deployment", "drift-mcp"), kinds)
        self.assertNotIn(("Deployment", "feature-rag-mcp"), kinds)
        self.assertNotIn("istio.io/dataplane-mode", find(self.docs, "Namespace", "ewallet-agents")["metadata"]["labels"])
        self.assertEqual(find(self.docs, "Deployment", "vllm-fallback")["spec"]["replicas"], 0)

    def test_vllm_serves_tool_calls_without_reasoning_leak(self):
        container = find(self.docs, "Deployment", "vllm-primary")["spec"]["template"]["spec"]["containers"][0]
        args = container["args"]
        self.assertIn("--enable-auto-tool-choice", args)
        self.assertEqual(args[args.index("--tool-call-parser") + 1], "hermes")
        self.assertEqual(args[args.index("--reasoning-parser") + 1], "qwen3")
        self.assertEqual(json.loads(args[args.index("--default-chat-template-kwargs") + 1]), {"enable_thinking": False})
        probe = container["startupProbe"]["exec"]["command"][2]
        self.assertIn('"tools"', probe)
        self.assertIn("/v1/chat/completions", probe)


class ToolCallValidationTests(unittest.TestCase):
    VALID = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "feature_lookup", "arguments": "{\"transaction_id\": \"tx-demo-001\"}"}}]}

    def test_valid_message(self):
        self.assertEqual(smoke.validate_tool_calls(self.VALID), [])

    def test_invalid_messages(self):
        def variant(**changes):
            message = json.loads(json.dumps(self.VALID))
            call = message["tool_calls"][0]
            for key, value in changes.items():
                if key == "content":
                    message["content"] = value
                elif key in ("name", "arguments"):
                    call["function"][key] = value
                else:
                    call[key] = value
            return message

        cases = {
            "no tool calls": {"role": "assistant", "content": "I would call a tool"},
            "missing id": variant(id=""),
            "wrong type": variant(type="tool"),
            "unknown tool": variant(name="shell"),
            "arguments not JSON": variant(arguments="{transaction_id: 1"),
            "arguments not a string": variant(arguments={"transaction_id": "x"}),
            "missing required": variant(arguments="{}"),
            "unexpected argument": variant(arguments="{\"transaction_id\": \"x\", \"amount\": 1}"),
            "markup leak": variant(content="<tool_call>{}</tool_call>"),
            "reasoning leak": variant(content="<think>plan</think>"),
        }
        for label, message in cases.items():
            with self.subTest(label):
                self.assertTrue(smoke.validate_tool_calls(message))

    def test_stream_chunks_assemble_into_one_tool_call(self):
        chunk = lambda delta, finish=None: "data: " + json.dumps(  # noqa: E731
            {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
        lines = [
            chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_9", "type": "function",
                                                        "function": {"name": "feature_lookup", "arguments": ""}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"transaction_"}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": "id\": \"tx-demo-001\"}"}}]}),
            chunk({}, "tool_calls"),
            "data: " + json.dumps({"choices": [], "usage": {"total_tokens": 49}}),
            "data: [DONE]",
        ]
        message, finish, usage = smoke.assemble_stream(lines)
        self.assertEqual((finish, usage), ("tool_calls", {"total_tokens": 49}))
        self.assertEqual(smoke.validate_tool_calls(message), [])
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"]), {"transaction_id": "tx-demo-001"})

    def test_cli_is_local_only_and_needs_a_key(self):
        with self.assertRaises(SystemExit):
            smoke.main(["--endpoint", "http://10.0.0.5:8000"])


class PreflightTests(unittest.TestCase):
    def test_context_must_be_explicit_and_local(self):
        for context, ok in ((None, False), ("", False),
                            ("gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev", False),
                            ("kind-edai-cluster", False), ("kind-llm-agent-local", True)):
            with self.subTest(context):
                self.assertEqual(r1_preflight.context_checks(context)[0]["status"] == "PASS", ok)

    def test_cluster_checks_never_read_secret_data(self):
        calls = []

        def run(args):
            calls.append(list(args))
            out = "Bound" if "pvc" in args else ""
            return subprocess.CompletedProcess(args, 0, out, "")

        results = r1_preflight.cluster_checks("kind-llm-agent-local", run)
        self.assertTrue(all(r["status"] == "PASS" for r in results))
        secret = next(c for c in calls if "secret" in c)
        self.assertEqual(secret[-2:], ["-o", "name"])
        self.assertTrue(all(c[:3] == ["kubectl", "--context", "kind-llm-agent-local"] for c in calls))

    def host(self, **changes):
        base = dict(meminfo="MemAvailable:   16777216 kB\n", loadavg="2.0 2.0 2.0 1/100 1", cpus=22,
                    processes="bash\npython -m http.server\n",
                    airflow_health={"metadatabase": {"status": "healthy"}, "scheduler": {"status": "healthy"}})
        base.update(changes)
        values = {"vllm": {"resources": {"limits": {"memory": "6Gi", "cpu": "3"}}}}
        return {r["check"]: r["status"] for r in r1_preflight.host_checks(values, **base)}

    def test_host_go_and_each_no_go(self):
        self.assertTrue(all(v == "PASS" for v in self.host().values()))
        self.assertEqual(self.host(processes="gcloud dataproc batches submit pyspark x")["dp2-terminal"], "FAIL")
        self.assertEqual(self.host(processes="python -m ops.gcp.dp2_method2 --root x")["dp2-terminal"], "FAIL")
        self.assertEqual(self.host(meminfo="MemAvailable:   12582912 kB\n")["memory-headroom"], "FAIL")
        self.assertEqual(self.host(loadavg="15.0 1 1 1/1 1")["cpu-load"], "FAIL")
        self.assertEqual(self.host(airflow_health=None)["airflow-healthy"], "FAIL")

    def test_node_cap(self):
        values = {"vllm": {"resources": {"limits": {"memory": "6Gi", "cpu": "3"}}}}
        ok = {r["check"]: r["status"] for r in r1_preflight.node_cap_checks(values, "4000000000 9663676416")}
        self.assertEqual(ok, {"kind-node-capped": "PASS", "vllm-fits-node": "PASS"})
        uncapped = {r["check"]: r["status"] for r in r1_preflight.node_cap_checks(values, "0 0")}
        self.assertEqual(uncapped["kind-node-capped"], "FAIL")

    @unittest.skipUnless(HELM, "helm binary unavailable")
    def test_chart_checks_pass_for_r1_and_catch_a_second_vllm_pod(self):
        files = [CHART / "values-r1-local.yaml"]
        docs, error = r1_preflight.render(files)
        self.assertEqual(error, "")
        results = r1_preflight.chart_checks(docs, r1_preflight.merged_values(files))
        self.assertEqual([r for r in results if r["status"] != "PASS"], [])
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as extra:
            yaml.safe_dump({"vllm": {"fallback": {"replicas": 1}}}, extra)
            extra.flush()
            files = [CHART / "values-r1-local.yaml", Path(extra.name)]
            docs, _ = r1_preflight.render(files)
            failed = {r["check"] for r in r1_preflight.chart_checks(docs, r1_preflight.merged_values(files))
                      if r["status"] == "FAIL"}
        self.assertEqual(failed, {"single-vllm-pod"})


class OfflineGatewayMockEvidenceTests(unittest.TestCase):
    """Real agentgateway binary + mock vLLM (scripts/llm_agent/r1_mock/run.sh). Not runtime evidence."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(EVIDENCE.read_text())

    def scenario(self, name):
        report = self.data[name]
        return report, {k: len(v) for k, v in report["backend_requests"].items()}

    def test_marked_as_offline_mock(self):
        self.assertEqual(self.data["_meta"]["kind"], "OFFLINE_MOCK_BACKEND")

    def test_routing_rewrites_model_and_backend_key(self):
        for name, stream in (("ok_non_stream", False), ("ok_stream", True)):
            report, hits = self.scenario(name)
            self.assertEqual((report["verdict"], report["first_turn"]["status"], report["first_turn"]["backend"]),
                             ("PASS", 200, "primary"))
            self.assertEqual(report["tool_call_errors"], [])
            seen = report["backend_requests"]["primary"]
            self.assertTrue(all(r["model"] == "ewallet-fraud-llm-v1" and r["auth_ok"] and r["stream"] == stream
                                for r in seen))
            self.assertEqual(seen[0]["tools"], 1)

    def test_slow_backend_is_a_single_504_not_a_replay(self):
        report, hits = self.scenario("primary_slow_beyond_backend_budget")
        self.assertEqual((report["first_turn"]["status"], hits["primary"]), (504, 1))
        self.assertLess(report["first_turn"]["latency_s"], 5.5)  # listener backend budget (4 s), not entry (6 s)

    def test_errors_propagate_and_fail_closed(self):
        report, hits = self.scenario("primary_500_not_replayed")
        self.assertEqual((report["first_turn"]["status"], hits["primary"]), (500, 1))
        report, hits = self.scenario("primary_down_503")
        self.assertEqual((report["first_turn"]["status"], hits["primary"]), (503, 2))  # one retry on 503
        report, _ = self.scenario("all_backends_down")
        self.assertEqual((report["verdict"], report["first_turn"]["status"]), ("FAIL", 503))
        self.assertNotIn("tool_calls", report["first_turn"])

    def test_failover_after_eviction(self):
        report, hits = self.scenario("primary_down_then_failover")
        self.assertEqual(report["preceding_request_status"], 503)
        self.assertEqual((report["verdict"], report["first_turn"]["backend"]), ("PASS", "fallback"))


if __name__ == "__main__":
    unittest.main()
