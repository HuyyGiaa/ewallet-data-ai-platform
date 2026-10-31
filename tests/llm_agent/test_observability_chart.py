"""OpenTelemetry Collector, redaction and dashboard source contract."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

from ops.llm_agent.policy_lint import baseline_violations, flow_violations

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
EVIDENCE = ROOT / "docs/evidence/llm_agent/source_review"
HELM = shutil.which("helm")
CADVISOR = {"container_cpu_usage_seconds_total", "container_memory_working_set_bytes"}


def render(*overrides):
    with tempfile.TemporaryDirectory() as folder:
        args = [HELM, "template", "llm-agent", str(CHART), "-f", str(CHART / "values-gcp.yaml")]
        for index, override in enumerate(overrides):
            path = Path(folder) / f"override-{index}.yaml"
            path.write_text(yaml.safe_dump(override))
            args += ["-f", str(path)]
        proc = subprocess.run(args, capture_output=True, text=True, check=True)
    return [item for item in yaml.safe_load_all(proc.stdout) if item]


def collector_config(items):
    configmap = next(i for i in items if i["kind"] == "ConfigMap" and i["metadata"]["name"] == "otel-collector-config")
    return yaml.safe_load(configmap["data"]["config.yaml"])


@unittest.skipUnless(HELM, "helm binary unavailable")
class CollectorTests(unittest.TestCase):
    ON = {"otelCollector": {"enabled": True}}

    def test_disabled_by_default(self):
        self.assertFalse([i for i in render() if i["kind"] == "Deployment" and i["metadata"]["name"] == "otel-collector"])

    def test_every_pipeline_redacts_before_export(self):
        config = collector_config(render(self.ON))
        pipelines = config["service"]["pipelines"]
        for name, pipeline in pipelines.items():
            with self.subTest(name):
                processors = pipeline["processors"]
                self.assertEqual(processors[0], "memory_limiter")
                self.assertIn("attributes/redact", processors)
                self.assertEqual(processors[-1], "batch")
        for name in ("traces", "logs"):
            self.assertIn("redaction", pipelines[name]["processors"])
        deleted = {a["key"] for a in config["processors"]["attributes/redact"]["actions"] if a["action"] == "delete"}
        self.assertTrue({"http.request.header.authorization", "gen_ai.prompt", "gen_ai.completion"} <= deleted)
        for pattern in config["processors"]["redaction"]["blocked_values"]:
            re.compile(pattern)

    def test_traces_go_to_the_backend_only_when_configured(self):
        default = collector_config(render(self.ON))["service"]["pipelines"]["traces"]["exporters"]
        self.assertEqual(default, ["debug", "spanmetrics"])
        backend = collector_config(render({"otelCollector": {"enabled": True,
                                                             "tracesEndpoint": "jaeger.observability:4317"}}))
        self.assertEqual(backend["exporters"]["otlp/traces"]["endpoint"], "jaeger.observability:4317")
        self.assertIn("spanmetrics", backend["service"]["pipelines"]["metrics"]["receivers"])

    def test_exporter_flows_are_enforced_and_workload_hardened(self):
        items = render(self.ON, {"mcpGateway": {"enabled": True}, "a2aGateway": {"enabled": True}, "llmd": {"enabled": True}})
        self.assertEqual(flow_violations(items, namespace="ewallet-agents"), [])
        self.assertEqual(baseline_violations(items), [])
        policies = {i["metadata"]["name"] for i in items if i["kind"] == "AuthorizationPolicy"}
        for source in ("agentgateway", "mcp-gateway", "a2a-gateway", "feature-rag-mcp", "drift-mcp", "llmd-epp"):
            self.assertIn(f"allow-{source}-to-otel-collector", policies)
        pod = next(i for i in items if i["kind"] == "Deployment" and i["metadata"]["name"] == "otel-collector")
        self.assertRegex(pod["spec"]["template"]["spec"]["containers"][0]["image"], r"@sha256:[0-9a-f]{64}$")


class DashboardTests(unittest.TestCase):
    def test_verified_panels_use_metric_names_read_from_the_binary(self):
        dashboard = json.loads((CHART / "dashboards/llm-agent.json").read_text())
        observed = set(re.findall(r"^(agentgateway_\w+)",
                                  (EVIDENCE / "campaign_20261031/agentgateway_metric_names.txt").read_text(), re.M))
        bases = {re.sub(r"_(count|sum|bucket|total)$", "", name) for name in observed}
        covered = []
        for panel in dashboard["panels"]:
            status = panel["ewallet"]["metric_status"]
            self.assertIn(status, ("verified", "unverified"))
            names = set(re.findall(r"\b((?:agentgateway|container|ewallet)_\w+)", panel["targets"][0]["expr"]))
            self.assertTrue(names, panel["title"])
            if status == "verified":
                for name in names:
                    base = re.sub(r"_(count|sum|bucket|total)$", "", name)
                    self.assertTrue(name in CADVISOR or base in bases, (panel["title"], name))
            else:
                self.assertIn("UNVERIFIED", panel["description"])
            covered.append(panel["title"])
        self.assertGreaterEqual(len(covered), 12)

    def test_redaction_smoke_evidence_shows_no_leak(self):
        smoke = json.loads((EVIDENCE / "campaign_20261031/otel_redaction_smoke.json").read_text())
        self.assertEqual((smoke["verdict"], smoke["leaked"], smoke["span_exported"]), ("PASS", [], True))

    def test_collector_configs_passed_the_pinned_binary(self):
        lines = (EVIDENCE / "gateways/agentgateway_validate_only.txt").read_text().splitlines()
        self.assertIn("collector-debug: valid", lines)
        self.assertIn("collector-traces: valid", lines)


if __name__ == "__main__":
    unittest.main()
