"""Contracts for the bounded observability foundation."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from ops.llm_agent.policy_lint import flow_violations


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"


class FoundationTests(unittest.TestCase):
    def test_stack_is_opt_in_and_resource_bounded(self) -> None:
        values = yaml.safe_load((CHART / "values.yaml").read_text())
        stack = values["observabilityStack"]
        self.assertFalse(stack["enabled"])
        for component in ("prometheus", "loki", "tempo", "grafana"):
            config = stack[component]
            self.assertRegex(config["image"], r"@sha256:[0-9a-f]{64}$")
            self.assertIn("requests", config["resources"])
            self.assertIn("limits", config["resources"])
        template = (CHART / "templates/observability-stack.yaml").read_text()
        self.assertIn("{{- if .Values.observabilityStack.enabled }}", template)
        self.assertIn("runAsNonRoot: true", template)
        self.assertIn("readOnlyRootFilesystem: true", template)
        self.assertIn("adminSecretName", template)
        self.assertIn("GF_SECURITY_ADMIN_PASSWORD", template)
        self.assertIn("secretKeyRef", template)
        self.assertIn("GF_PATHS_DATA", template)

    def test_prometheus_scrapes_required_real_sources(self) -> None:
        template = (CHART / "templates/observability-stack.yaml").read_text()
        for job in (
            "otel-collector",
            "agentgateway",
            "vllm",
            "kubernetes-cadvisor",
            "kube-state-metrics",
        ):
            self.assertIn(f"job_name: {job}", template)
        self.assertIn("agentgateway:15020", template)
        gateway = (CHART / "templates/agentgateway.yaml").read_text()
        self.assertIn("name: metrics", gateway)
        self.assertIn("15020", gateway)
        self.assertIn("metrics_path: /metrics", template)
        self.assertIn("name: otlp-grpc", template)
        self.assertIn("port: 4317", template)
        prometheus_config = template.split("prometheus.yaml: |", 1)[1].split("loki.yaml: |", 1)[0]
        self.assertNotIn("\n    storage:", prometheus_config)
        self.assertNotIn("request_id", template)
        self.assertNotIn("transaction_id", template)
        self.assertNotIn("user_id", template)

    def test_collector_has_bounded_log_and_trace_exports(self) -> None:
        values = yaml.safe_load((CHART / "values.yaml").read_text())
        collector = values["otelCollector"]
        self.assertEqual(collector["logsEndpoint"], "")
        self.assertEqual(collector["tracesEndpoint"], "")
        text = (CHART / "templates/otel-collector.yaml").read_text()
        self.assertLess(text.index("memory_limiter"), text.index("attributes/redact"))
        self.assertLess(text.index("attributes/redact"), text.index("batch:"))
        self.assertIn("otlphttp/logs", text)
        self.assertIn("otlp/traces", text)
        self.assertIn("limit_percentage: 80", text)

    def test_platform_dashboard_preserves_missing_telemetry(self) -> None:
        dashboard = json.loads((CHART / "dashboards/platform-overview.json").read_text())
        self.assertEqual(dashboard["uid"], "ewallet-platform-overview")
        expressions = "\n".join(
            target["expr"] for panel in dashboard["panels"] for target in panel["targets"]
        )
        for metric in (
            "agentgateway_requests_total",
            "agentgateway_request_duration_seconds_bucket",
            "container_cpu_usage_seconds_total",
            "container_memory_working_set_bytes",
            "container_network_receive_bytes_total",
            "container_fs_usage_bytes",
            "kube_pod_container_status_restarts_total",
            "kube_pod_container_status_last_terminated_reason",
            "kube_pod_container_resource_requests",
            "kube_pod_container_resource_limits",
        ):
            self.assertIn(metric, expressions)
        self.assertNotIn("vector(0)", expressions)
        self.assertNotIn(" or ", expressions)

    @unittest.skipUnless(shutil.which("helm"), "helm binary unavailable")
    def test_stack_internal_flows_survive_default_deny(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            override = Path(folder) / "stack.yaml"
            override.write_text(yaml.safe_dump({
                "observabilityStack": {"enabled": True},
                "otelCollector": {"enabled": True, "logsEndpoint": "http://loki:3100/otlp",
                                  "tracesEndpoint": "tempo:4317"},
                "dashboards": {"enabled": True},
            }))
            rendered = subprocess.run(
                [shutil.which("helm"), "template", "llm-agent", str(CHART),
                 "-f", str(CHART / "values-local.yaml"), "-f", str(override)],
                check=True, capture_output=True, text=True,
            ).stdout
        items = [item for item in yaml.safe_load_all(rendered) if item]
        self.assertEqual(flow_violations(items, namespace="ewallet-agents"), [])
        enforced = json.loads(next(
            item for item in items
            if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "llm-agent-network-flows"
        )["data"]["enforced.json"])
        names = {flow["name"] for flow in enforced}
        self.assertTrue({
            "prometheus-to-agentgateway", "prometheus-to-vllm",
            "prometheus-to-otel-collector", "otel-collector-to-loki",
            "otel-collector-to-tempo", "grafana-to-prometheus",
            "grafana-to-loki", "grafana-to-tempo",
        } <= names)


if __name__ == "__main__":
    unittest.main()
