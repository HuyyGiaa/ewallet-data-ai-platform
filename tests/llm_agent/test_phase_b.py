from __future__ import annotations

import subprocess
from pathlib import Path
import unittest

import yaml

from ai_platform.llm_agent.benchmark import (
    RequestMeasurement,
    ResourceMeasurement,
    summarize,
)
from ops.llm_agent.run_llm_benchmark import parse_cpu, parse_memory, parse_sse


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"


def render() -> list[dict]:
    output = subprocess.run(
        ["helm", "template", "llm-agent", str(CHART), "-f", str(CHART / "values-gcp.yaml")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [item for item in yaml.safe_load_all(output) if item]


class PhaseBManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = render()

    def test_vllm_is_cpu_first_pinned_and_bounded(self) -> None:
        deployments = {
            item["metadata"]["name"]: item
            for item in self.documents
            if item["kind"] == "Deployment"
        }
        for name in ("vllm-primary", "vllm-fallback"):
            container = deployments[name]["spec"]["template"]["spec"]["containers"][0]
            self.assertIn("vllm-openai-cpu:v0.32.0-x86_64@sha256:", container["image"])
            self.assertIn("--revision", container["args"])
            self.assertIn("--max-num-seqs", container["args"])
            self.assertIn("--max-num-queued-reqs", container["args"])
            self.assertIn("startupProbe", container)
            self.assertIn("limits", container["resources"])
        self.assertIn(
            "--enable-prefix-caching",
            deployments["vllm-primary"]["spec"]["template"]["spec"]["containers"][0]["args"],
        )
        self.assertNotIn(
            "--enable-prefix-caching",
            deployments["vllm-fallback"]["spec"]["template"]["spec"]["containers"][0]["args"],
        )

    def test_agentgateway_has_timeout_retry_quota_circuit_and_fallback(self) -> None:
        config_map = next(
            item for item in self.documents
            if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "agentgateway-llm-config"
        )
        config = yaml.safe_load(config_map["data"]["config.yaml"])
        policies = config["llm"]["policies"]
        route_policies = config["routes"][0]["policies"]
        # One retry, only for an unreached/refusing backend: a timed-out CPU
        # generation is never replayed (R1 prep, 2026-10-31).
        self.assertEqual((route_policies["retry"]["attempts"], route_policies["retry"]["codes"]), (1, [502, 503]))
        self.assertNotIn("backendRequestTimeout", route_policies["timeout"])
        self.assertEqual(route_policies["timeout"]["requestTimeout"], "30s")
        self.assertEqual(config["routes"][0]["backends"][0]["host"], "127.0.0.1:3000")
        self.assertEqual({item["type"] for item in policies["localRateLimit"]}, {"requests", "tokens"})
        models = config["llm"]["models"]
        self.assertTrue(all("custom" in model["provider"] for model in models))
        self.assertTrue(all(model["health"]["eviction"]["consecutiveFailures"] == 2 for model in models))
        targets = config["llm"]["virtualModels"][0]["routing"]["failover"]["targets"]
        self.assertEqual([target["priority"] for target in targets], [0, 1])

    def test_global_modelconfig_routes_only_through_agentgateway(self) -> None:
        model = next(item for item in self.documents if item["kind"] == "ModelConfig")
        self.assertEqual(model["apiVersion"], "kagent.dev/v1alpha2")
        self.assertEqual(model["spec"]["model"], "ewallet-fraud-llm")
        self.assertEqual(model["spec"]["apiKeySecret"], "vllm-api-key")
        # The client budget exceeds the gateway entry budget (30 s), so the gateway answers first.
        self.assertEqual(model["spec"]["openAI"]["timeout"], 35)
        self.assertNotIn("stream", model["spec"])
        self.assertEqual(
            model["spec"]["openAI"]["baseUrl"],
            "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1",
        )
        self.assertNotIn("vllm", model["spec"]["openAI"]["baseUrl"])


class BenchmarkContractTest(unittest.TestCase):
    def test_stream_and_resource_parsers(self) -> None:
        import json
        import time

        started = time.perf_counter() - 0.01
        lines = [
            b'data: {"choices":[{"delta":{"content":"A"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"B"}}]}\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n',
            b'data: [DONE]\n',
        ]
        ttft, intervals, usage = parse_sse(lines, started=started)
        self.assertGreater(ttft, 0)
        self.assertEqual(len(intervals), 1)
        self.assertEqual(usage["total_tokens"], 5)
        self.assertEqual(parse_cpu("250m"), 250)
        self.assertEqual(parse_cpu("2"), 2000)
        self.assertEqual(parse_memory("2Gi"), 2048)
        self.assertEqual(parse_memory("512Mi"), 512)

    def test_summary_contains_every_required_metric_family(self) -> None:
        rows = [
            RequestMeasurement(
                request_id=f"req-{index}",
                backend="primary" if index < 2 else "fallback",
                model="ewallet-fraud-llm-v1",
                prompt_revision="prompt-v1",
                e2e_ms=100 + index * 10,
                ttft_ms=20 + index,
                inter_token_latencies_ms=(5.0, 6.0),
                input_tokens=10,
                output_tokens=4,
                total_tokens=14,
                error=False,
                fallback=index == 2,
            )
            for index in range(3)
        ]
        result = summarize(
            rows,
            [ResourceMeasurement(900, 2048), ResourceMeasurement(1100, 2200)],
            wall_seconds=2.0,
        )
        self.assertEqual(result["measurement_kind"], "actual")
        self.assertEqual(set(result["latency_ms"]), {"p50", "p95", "p99"})
        self.assertIn("p95", result["ttft_ms"])
        self.assertIn("p95", result["itl_ms"])
        self.assertEqual(result["tokens"]["total"], 42)
        self.assertAlmostEqual(result["fallback_rate"], 1 / 3)
        self.assertEqual(result["resources"]["memory_mib_peak"], 2200)

    def test_planned_or_missing_resource_data_cannot_be_summarized(self) -> None:
        with self.assertRaisesRegex(ValueError, "actual request"):
            summarize([], [], wall_seconds=1.0)


if __name__ == "__main__":
    unittest.main()
