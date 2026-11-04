"""N12 chart contracts: computing metrics, pod-log forwarder, agent metrics switch."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from tests.llm_agent.test_agents_chart import HELM, ON, render
from tests.llm_agent.test_n11_chart import BYO, env, keyed

STACK = {"observabilityStack": {"enabled": True}, "otelCollector": {"enabled": True}}
CIDRS = ["34.118.224.1/32", "10.20.0.8/32"]
COMPUTE = {"observabilityStack": {"clusterMetrics": {"enabled": True, "apiServer": {"cidrs": CIDRS},
                                                     "kubelet": {"cidrs": ["10.20.0.0/20"]}}}}
ALLOY = "docker.io/grafana/alloy:v1.11.3@sha256:" + "8" * 64
LOGS = {"podLogs": {"enabled": True, "image": ALLOY, "apiServer": {"cidrs": CIDRS}}}
ALLOY_BIN = shutil.which("alloy")


def by(items, kind):
    return {(item["metadata"].get("namespace"), item["metadata"]["name"]): item for item in items if item["kind"] == kind}


@unittest.skipUnless(HELM, "helm binary unavailable")
class ComputeMetricsChartTests(unittest.TestCase):
    def test_rbac_is_limited_to_nodes_and_the_kubelet_metrics_subresource(self):
        role = by(render(ON, BYO, STACK, COMPUTE), "ClusterRole")[(None, "llm-agent-observability-prometheus")]
        self.assertEqual(role["rules"], [
            {"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list", "watch"]},
            {"apiGroups": [""], "resources": ["nodes/metrics"], "verbs": ["get"]},
        ])
        self.assertNotIn("nodes/proxy", yaml.safe_dump(role))

    def test_kubelets_are_scraped_directly_with_verified_tls(self):
        config = by(render(ON, BYO, STACK, COMPUTE), "ConfigMap")[("ewallet-agents", "observability-config")]
        jobs = {job["job_name"]: job for job in yaml.safe_load(config["data"]["prometheus.yaml"])["scrape_configs"]}
        for name, path in (("kubernetes-cadvisor", "/metrics/cadvisor"), ("kubernetes-kubelet-volumes", "/metrics")):
            job = jobs[name]
            self.assertEqual((job["scheme"], job["metrics_path"]), ("https", path))
            self.assertFalse(job["tls_config"]["insecure_skip_verify"])
            self.assertEqual(job["tls_config"]["ca_file"], "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
            self.assertNotIn("proxy", yaml.safe_dump(job))

    def test_scrape_jobs_keep_only_compute_series_and_skip_missing_kube_state_metrics(self):
        config = by(render(ON, BYO, STACK, COMPUTE), "ConfigMap")[("ewallet-agents", "observability-config")]
        prom = yaml.safe_load(config["data"]["prometheus.yaml"])
        jobs = {job["job_name"]: job for job in prom["scrape_configs"]}
        self.assertIn("kubernetes-cadvisor", jobs)
        self.assertIn("kubernetes-kubelet-volumes", jobs)
        self.assertNotIn("kube-state-metrics", jobs)
        keep = jobs["kubernetes-cadvisor"]["metric_relabel_configs"][0]["regex"]
        for name in ("container_cpu_usage_seconds_total", "container_memory_working_set_bytes",
                     "container_network_receive_bytes_total", "container_network_transmit_bytes_total",
                     "container_fs_reads_bytes_total", "container_fs_usage_bytes"):
            self.assertRegex(name, f"^(?:{keep})$")
        self.assertNotRegex("container_tasks_state", f"^(?:{keep})$")

    def test_apiserver_egress_is_exact_and_required(self):
        policy = by(render(ON, BYO, STACK, COMPUTE), "NetworkPolicy")[
            ("ewallet-agents", "allow-observability-prometheus-to-kube-apiserver")]
        egress = policy["spec"]["egress"]
        self.assertEqual([peer["ipBlock"]["cidr"] for peer in egress[0]["to"]], CIDRS)
        self.assertEqual(egress[0]["ports"], [{"protocol": "TCP", "port": 443}])
        self.assertEqual([peer["ipBlock"]["cidr"] for peer in egress[1]["to"]], ["10.20.0.0/20"])
        self.assertEqual(egress[1]["ports"], [{"protocol": "TCP", "port": 10250}])
        self.assertEqual(len(egress), 2)
        for missing in ({"apiServer": {"cidrs": CIDRS}}, {"kubelet": {"cidrs": ["10.20.0.0/20"]}}):
            partial = {"observabilityStack": {"clusterMetrics": {"enabled": True, **missing}}}
            self.assertNotEqual(render(ON, BYO, STACK, {"observabilityStack": {"clusterMetrics": None}}, partial,
                                       check=False).returncode, 0)


@unittest.skipUnless(HELM, "helm binary unavailable")
class PodLogsChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = render(ON, BYO, STACK, LOGS)

    def test_disabled_by_default(self):
        names = {(item["kind"], item["metadata"]["name"]) for item in render(ON, BYO, STACK)}
        self.assertFalse({name for name in names if "alloy" in name[1]})

    def test_namespaced_read_only_rbac_for_the_three_source_namespaces(self):
        roles = by(self.items, "Role")
        self.assertEqual({key for key in roles if key[1] == "observability-alloy-pod-logs"},
                         {("ewallet-agents", "observability-alloy-pod-logs"),
                          ("ml-platform", "observability-alloy-pod-logs"),
                          ("fraud-serving", "observability-alloy-pod-logs")})
        for role in (roles[key] for key in roles if key[1] == "observability-alloy-pod-logs"):
            self.assertEqual(role["rules"], [
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
            ])
        self.assertFalse([item for item in self.items if item["kind"] in ("ClusterRole", "ClusterRoleBinding")
                          and "alloy" in item["metadata"]["name"]])

    def test_pod_is_baseline_compliant_without_host_access(self):
        deployment = by(self.items, "Deployment")[("ewallet-agents", "observability-alloy")]
        spec = deployment["spec"]["template"]["spec"]
        self.assertTrue(spec["securityContext"]["runAsNonRoot"])
        self.assertFalse(any("hostPath" in volume for volume in spec["volumes"]))
        self.assertFalse(spec.get("hostNetwork") or spec.get("hostPID"))
        container = spec["containers"][0]
        self.assertEqual(container["image"], ALLOY)
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})

    def test_loki_flow_and_apiserver_egress_are_generated(self):
        policies = by(self.items, "NetworkPolicy")
        self.assertIn(("ewallet-agents", "allow-observability-alloy-to-loki-ingress"), policies)
        self.assertIn(("ewallet-agents", "allow-observability-alloy-to-loki-egress"), policies)
        egress = policies[("ewallet-agents", "allow-observability-alloy-to-kube-apiserver")]["spec"]["egress"][0]
        self.assertEqual([peer["ipBlock"]["cidr"] for peer in egress["to"]], CIDRS)
        authz = by(self.items, "AuthorizationPolicy")[("ewallet-agents", "allow-observability-alloy-to-loki")]
        self.assertEqual(authz["spec"]["rules"][0]["from"][0]["source"]["principals"],
                         ["cluster.local/ns/ewallet-agents/sa/observability-alloy"])

    def test_config_selects_only_the_named_workloads_and_redacts(self):
        config = by(self.items, "ConfigMap")[("ewallet-agents", "observability-alloy-config")]["data"]["config.alloy"]
        self.assertIn('"ewallet-agents;(agentgateway|mcp-gateway|a2a-gateway)"', config)
        self.assertIn('"ml-platform;(fraud-api)"', config)
        self.assertIn('"fraud-serving;(fraud-detector)"', config)
        self.assertIn("http://loki.ewallet-agents.svc.cluster.local:3100/loki/api/v1/push", config)
        self.assertEqual(config.count("stage.replace"), 4)
        self.assertNotRegex(config, r"fraud-coordinator|feature-rag|vllm")

    @unittest.skipUnless(ALLOY_BIN, "alloy binary unavailable")
    def test_rendered_config_is_valid_alloy_syntax(self):
        config = by(self.items, "ConfigMap")[("ewallet-agents", "observability-alloy-config")]["data"]["config.alloy"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.alloy"
            path.write_text(config)
            proc = subprocess.run([ALLOY_BIN, "fmt", str(path)], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_image_must_be_digest_pinned(self):
        bad = {"podLogs": {"image": "docker.io/grafana/alloy:latest"}}
        self.assertNotEqual(render(ON, BYO, STACK, LOGS, bad, check=False).returncode, 0)


@unittest.skipUnless(HELM, "helm binary unavailable")
class AgentMetricsChartTests(unittest.TestCase):
    def test_metrics_switch_changes_only_the_two_byo_agents(self):
        on = {"agents": {"byoTelemetry": {"metrics": {"enabled": True}}}}
        before, after = keyed(render(ON, BYO)), keyed(render(ON, BYO, on))
        self.assertEqual(sorted(key for key in before if before[key] != after[key]),
                         [("Agent", "ewallet-agents", "feature-rag-specialist"),
                          ("Agent", "ewallet-agents", "fraud-coordinator")])
        for name in ("fraud-coordinator", "feature-rag-specialist"):
            values = env(after[("Agent", "ewallet-agents", name)])
            self.assertEqual(values["EWALLET_OTEL_METRICS_ENABLED"]["value"], "true")
            self.assertEqual(values["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"]["value"],
                             "otel-collector.ewallet-agents.svc.cluster.local:4317")


if __name__ == "__main__":
    unittest.main()
