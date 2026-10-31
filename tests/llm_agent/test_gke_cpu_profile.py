"""GKE CPU runtime profile: overlay, Istio ambient install script, stage preflight.

All offline: the preflight decision runs on fixture facts, the Istio script on
fake helm/kubectl/curl and locally built chart files.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

from ops.llm_agent import gke_preflight as pf
from tests.llm_agent.test_ci_scripts import ScriptHarness

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
ISTIO = ROOT / "infra/kubernetes/istio-ambient"
HELM = shutil.which("helm")
MODEL_IMAGE = ("asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/vllm-cpu-model@sha256:"
               + "a" * 64)


def facts(**override):
    base = {"context": pf.EXPECTED_CONTEXT, "server_host": "gke-x.asia-southeast1-b.gke.goog",
            "cluster_endpoints": ["34.0.0.1", "gke-x.asia-southeast1-b.gke.goog"],
            "networkpolicy_enforced": True, "ready_nodes": 2, "allocatable": [7.84, 26.0],
            "existing_requests": [1.5, 5.0], "planned_requests": [4.5, 9.0],
            "crds": set(pf.stage_crds("g3")), "api_key_secret": True, "render_error": ""}
    return {**base, **override}


class PreflightDecisionTests(unittest.TestCase):
    def test_go_when_every_check_holds(self):
        for stage in pf.ORDER:
            with self.subTest(stage):
                self.assertEqual(pf.evaluate(facts(), stage), [])

    def test_each_failed_check_is_a_no_go(self):
        cases = {
            "default or other context": facts(context="kind-llm-agent-local"),
            "server is not this cluster": facts(server_host="10.9.9.9"),
            "networkpolicy not enforced": facts(networkpolicy_enforced=False),
            "no nodes": facts(ready_nodes=0, allocatable=[0.0, 0.0]),
            "cpu over headroom": facts(planned_requests=[6.0, 9.0]),
            "memory over headroom": facts(planned_requests=[4.5, 18.0]),
            "missing crd": facts(crds=set()),
            "missing api key secret": facts(api_key_secret=False),
            "stage does not render": facts(render_error="vllm.image must be the derived image"),
        }
        for label, value in cases.items():
            with self.subTest(label):
                self.assertTrue(pf.evaluate(value, "g1"))

    def test_networkpolicy_waiver_is_explicit(self):
        self.assertEqual(pf.evaluate(facts(networkpolicy_enforced=False), "g1", waive_networkpolicy=True), [])

    def test_stage_crds_accumulate(self):
        crds = set(pf.stage_crds("g1"))
        self.assertEqual(pf.evaluate(facts(crds=crds), "g1"), [])
        self.assertTrue(any("inferencepools" in r for r in pf.evaluate(facts(crds=crds), "g2")))

    def test_g0_needs_no_secret(self):
        self.assertEqual(pf.evaluate(facts(api_key_secret=False, crds=set()), "g0"), [])

    def test_quantities(self):
        self.assertEqual(pf.cpu("3920m"), 3.92)
        self.assertAlmostEqual(pf.mem_gib("13336472Ki"), 12.72, places=2)
        self.assertEqual(pf.mem_gib("4Gi"), 4.0)


class OverlayTests(unittest.TestCase):
    values = yaml.safe_load((CHART / "values-gke-cpu.yaml").read_text())

    def test_no_external_exposure_and_real_fallback(self):
        self.assertFalse(self.values["gateway"]["enabled"])
        self.assertEqual(self.values["vllm"]["fallback"]["replicas"], 1)
        self.assertEqual(self.values["vllm"]["rollingUpdate"]["maxSurge"], 0)

    def test_keeps_the_pinned_cpu_image_and_model(self):
        base = yaml.safe_load((CHART / "values.yaml").read_text())["vllm"]
        for key in ("model", "modelRevision"):
            self.assertNotIn(key, self.values["vllm"])
        # The derived model image is set at deploy time from its pushed digest.
        self.assertEqual(self.values["vllm"]["image"], "")
        self.assertIn("vllm-openai-cpu", base["image"])
        self.assertNotIn("nvidia.com/gpu", (CHART / "values-gke-cpu.yaml").read_text())

    @unittest.skipUnless(HELM, "helm binary unavailable")
    def test_g1_renders_two_cpu_vllm_pods_within_two_nodes(self):
        rendered = render().stdout
        kinds = {d["metadata"]["name"]: d for d in yaml.safe_load_all(rendered) if d and d["kind"] == "Deployment"}
        self.assertEqual(kinds["vllm-primary"]["spec"]["replicas"], 1)
        self.assertEqual(kinds["vllm-fallback"]["spec"]["replicas"], 1)
        cpu, mem = pf.planned_requests(rendered)
        # Two e2-standard-4 (2 x 3.92 CPU, 2 x ~13 GiB), with headroom for system pods.
        self.assertLessEqual(cpu, 5.0)
        self.assertLessEqual(mem, 10.0)
        self.assertNotIn("kind: Gateway\n", rendered)


def render(*overrides, image=MODEL_IMAGE):
    args = [HELM, "template", "x", str(CHART), "-f", str(CHART / "values-gke-cpu.yaml")]
    for item in ((f"vllm.image={image}",) if image else ()) + overrides:
        args += ["--set", item]
    return subprocess.run(args, capture_output=True, text=True, check=False)


def network_policies(*overrides):
    rendered = render(*overrides)
    assert rendered.returncode == 0, rendered.stderr
    return {d["metadata"]["name"]: d["spec"] for d in yaml.safe_load_all(rendered.stdout) if d and d["kind"] == "NetworkPolicy"}


@unittest.skipUnless(HELM, "helm binary unavailable")
class EnforcedNetworkPolicyTests(unittest.TestCase):
    """What G1 needs once GKE enforces NetworkPolicy under Istio ambient."""

    def test_ambient_flows_open_hbone_on_both_ends(self):
        policies = network_policies()
        for name, key in (("allow-agentgateway-to-vllm-ingress", "ingress"), ("allow-agentgateway-to-vllm-egress", "egress")):
            ports = {p["port"] for p in policies[name][key][0]["ports"]}
            self.assertEqual(ports, {8000, 15008})

    def test_kubelet_probes_only_from_the_ztunnel_snat_address_and_probe_port(self):
        policies = network_policies()
        expected = {"vllm": 8000, "agentgateway": 4000}
        for workload, port in expected.items():
            spec = policies[f"allow-ambient-probes-{workload}"]
            self.assertEqual(spec["ingress"], [{"from": [{"ipBlock": {"cidr": "169.254.7.127/32"}}],
                                                "ports": [{"protocol": "TCP", "port": port}]}])

    def test_dns_includes_nodelocal_dnscache(self):
        targets = network_policies()["allow-cluster-dns"]["egress"][0]["to"]
        self.assertIn({"ipBlock": {"cidr": "169.254.20.10/32"}}, targets)

    def test_no_internet_egress_is_rendered(self):
        for name, spec in network_policies().items():
            for rule in spec.get("egress", []):
                for peer in rule["to"]:
                    cidr = (peer.get("ipBlock") or {}).get("cidr", "")
                    self.assertFalse(cidr.startswith("0.0.0.0"), name)

    def test_without_ambient_no_hbone_or_probe_rules(self):
        policies = network_policies("namespace.ambient=false")
        self.assertFalse([n for n in policies if n.startswith("allow-ambient-probes-")])
        ports = {p["port"] for p in policies["allow-agentgateway-to-vllm-ingress"]["ingress"][0]["ports"]}
        self.assertEqual(ports, {8000})


@unittest.skipUnless(HELM, "helm binary unavailable")
class ModelImageModeTests(unittest.TestCase):
    def test_serves_the_baked_revision_offline(self):
        docs = [d for d in yaml.safe_load_all(render().stdout) if d and d["kind"] == "Deployment"]
        vllm = next(d for d in docs if d["metadata"]["name"] == "vllm-primary")["spec"]["template"]["spec"]
        container = vllm["containers"][0]
        self.assertEqual(container["image"], MODEL_IMAGE)
        self.assertEqual(container["args"][1], "/models/Qwen3-0.6B/c1899de289a04d12100db370d81485cdf75e47ca")
        self.assertNotIn("--revision", container["args"])
        env = {e["name"]: e.get("value") for e in container["env"]}
        self.assertEqual((env["HF_HUB_OFFLINE"], env["TRANSFORMERS_OFFLINE"]), ("1", "1"))
        self.assertNotIn("persistentVolumeClaim", json_dump(vllm.get("volumes", [])))

    def test_refuses_without_the_derived_image_by_digest(self):
        cases = {"no image": "", "upstream image": yaml.safe_load((CHART / "values.yaml").read_text())["vllm"]["image"],
                 "tag only": "asia-southeast1-docker.pkg.dev/p/r/vllm-cpu-model:abc"}
        for label, image in cases.items():
            with self.subTest(label):
                self.assertNotEqual(render(image=image).returncode, 0)
        self.assertNotEqual(render("vllm.modelSource.image.path=/models/Qwen3-0.6B/other").returncode, 0)

    def test_recipe_pins_the_same_base_and_checksums(self):
        dockerfile = (ROOT / "infra/docker/vllm-cpu-model/Dockerfile").read_text()
        base = yaml.safe_load((CHART / "values.yaml").read_text())["vllm"]["image"]
        self.assertIn(f"FROM {base} AS remediated\n", dockerfile)
        self.assertIn("FROM remediated\n", dockerfile)

    def test_remediation_is_pinned_and_the_gate_stays_strict(self):
        lock = (ROOT / "infra/docker/vllm-cpu-model/uv.lock").read_text()
        self.assertRegex(lock, r'certifi-2026\.10\.18-py3-none-any\.whl", hash = "sha256:[0-9a-f]{64}"')
        self.assertRegex(lock, r'setuptools-82\.0\.1-py3-none-any\.whl", hash = "sha256:[0-9a-f]{64}"')
        script = (ROOT / "infra/docker/vllm-cpu-model/remediate.py").read_text()
        self.assertIn('sha256 does not match uv.lock', script)
        dockerfile = (ROOT / "infra/docker/vllm-cpu-model/Dockerfile").read_text()
        self.assertIn("libbrotli1=1.0.9-2ubuntu0.1", dockerfile)
        self.assertIn("--require-hashes --no-deps", dockerfile)
        gate = (ROOT / "scripts/llm_agent/scan_and_push.sh").read_text()
        self.assertIn("--scanners vuln,secret --exit-code 1 --severity CRITICAL,HIGH", gate)
        self.assertFalse(list((ROOT / "infra").glob("**/.trivyignore*")))


def json_dump(value):
    import json
    return json.dumps(value)


class ModelImageBuildScriptTests(ScriptHarness):
    def test_refuses_a_model_dir_that_does_not_match_the_checksums(self):
        model = self.dir / "model"
        model.mkdir()
        (model / "config.json").write_text("{}")
        proc = self.run_script("build_vllm_model_image.sh", model, "asia-southeast1-docker.pkg.dev/p/r",
                               "0" * 40, self.dir / "d.json")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse([c for c in self.calls() if c.startswith("docker build")])


class IstioProfileTests(unittest.TestCase):
    def test_pinned_and_gke_platform(self):
        env = dict(line.split("=", 1) for line in (ISTIO / "versions.env").read_text().splitlines()
                   if line and not line.startswith("#"))
        self.assertEqual(env["ISTIO_VERSION"], "1.30.7")
        for chart in ("BASE", "ISTIOD", "CNI", "ZTUNNEL"):
            self.assertRegex(env[f"ISTIO_{chart}_DIGEST"], r"^sha256:[0-9a-f]{64}$")
        values = yaml.safe_load((ISTIO / "values-gke.yaml").read_text())
        self.assertEqual((values["profile"], values["global"]["platform"]), ("ambient", "gke"))
        # Only the nested key takes effect in the 1.30.7 cni chart with platform=gke.
        self.assertEqual(values["cni"]["cniBinDir"], "/home/kubernetes/bin")
        self.assertNotIn("cniBinDir", values)


class IstioScriptTests(ScriptHarness):
    def setUp(self):
        super().setUp()
        self.charts = self.dir / "charts"
        self.charts.mkdir()
        lines = []
        for chart in ("base", "istiod", "cni", "ztunnel"):
            path = self.charts / f"{chart}-1.30.7.tgz"
            path.write_bytes(chart.encode())
            lines.append(f"ISTIO_{chart.upper()}_DIGEST=sha256:{hashlib.sha256(chart.encode()).hexdigest()}")
        # The script reads versions.env from the repo; mirror the repo layout with fixture digests.
        self.repo = self.dir / "repo"
        (self.repo / "scripts/llm_agent").mkdir(parents=True)
        (self.repo / "infra/kubernetes/istio-ambient").mkdir(parents=True)
        shutil.copy(ROOT / "scripts/llm_agent/istio_ambient_gke.sh", self.repo / "scripts/llm_agent/")
        shutil.copy(ISTIO / "values-gke.yaml", self.repo / "infra/kubernetes/istio-ambient/")
        for name, body in (("helm", 'echo "$*" >> "$FAKE_LOG"; [ "$1" = template ] && echo "            path: /home/kubernetes/bin"; exit 0'),
                           ("kubectl", 'echo "$*" | sed "s/^/kubectl /" >> "$FAKE_LOG"; case "$*" in *jsonpath*) printf "%s" "${FAKE_CNI_DIR:-/home/kubernetes/bin}";; esac; exit 0')):
            path = self.bin / name
            path.write_text("#!/usr/bin/env bash\n" + body + "\n")
        (self.repo / "infra/kubernetes/istio-ambient/versions.env").write_text(
            "ISTIO_VERSION=1.30.7\nISTIO_CHART_REPO=https://invalid.example\n" + "\n".join(lines) + "\n")

    def run_istio(self, mode, **env):
        return subprocess.run([str(self.repo / "scripts/llm_agent/istio_ambient_gke.sh"), mode, str(self.dir / "ev")],
                              capture_output=True, text=True, check=False,
                              env={**self.env, "ISTIO_CHART_DIR": str(self.charts), **env})

    def test_plan_renders_and_installs_nothing(self):
        proc = self.run_istio("plan", KUBE_CONTEXT=pf.EXPECTED_CONTEXT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len([c for c in self.calls() if c.startswith("template")]), 4)
        self.assertFalse([c for c in self.calls() if c.startswith("upgrade")])

    def test_apply_installs_in_order_with_explicit_context(self):
        proc = self.run_istio("apply", KUBE_CONTEXT=pf.EXPECTED_CONTEXT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        upgrades = [c for c in self.calls() if c.startswith("upgrade")]
        self.assertEqual([c.split()[2] for c in upgrades], ["istio-base", "istiod", "istio-cni", "ztunnel"])
        for call in upgrades:
            self.assertIn(f"--kube-context {pf.EXPECTED_CONTEXT}", call)
            self.assertIn("values-gke.yaml", call)

    def test_refuses_tampered_chart_and_wrong_context(self):
        (self.charts / "cni-1.30.7.tgz").write_bytes(b"tampered")
        self.assertNotEqual(self.run_istio("apply", KUBE_CONTEXT=pf.EXPECTED_CONTEXT).returncode, 0)
        (self.charts / "cni-1.30.7.tgz").write_bytes(b"cni")
        for env in ({}, {"KUBE_CONTEXT": "kind-llm-agent-local"}):
            with self.subTest(env=env):
                self.assertNotEqual(self.run_istio("apply", **env).returncode, 0)
        self.assertFalse([c for c in self.calls() if c.startswith("upgrade")])

    def test_apply_fails_when_the_installed_cni_dir_differs(self):
        proc = self.run_istio("apply", KUBE_CONTEXT=pf.EXPECTED_CONTEXT, FAKE_CNI_DIR="/opt/cni/bin")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("installed cni bin dir", proc.stderr)


if __name__ == "__main__":
    unittest.main()


class DeployModelImageTests(ScriptHarness):
    def digests(self, digest_ref):
        import json
        path = self.dir / "digests.json"
        path.write_text(json.dumps({"git_sha": "0" * 40, "images": {
            "vllm-cpu-model": {"tag_ref": "x", "digest_ref": digest_ref}}}))
        return path

    def test_gke_cpu_deploy_sets_the_model_image_digest(self):
        proc = self.run_script("deploy_atomic.sh", "gke-cpu", self.digests(MODEL_IMAGE), self.dir / "ev",
                               KUBE_CONTEXT=pf.EXPECTED_CONTEXT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        upgrade = next(c for c in self.calls() if c.startswith("helm upgrade"))
        self.assertIn(f"vllm.image={MODEL_IMAGE}", upgrade)
        self.assertIn("values-gke-cpu.yaml", upgrade)

    def test_tag_only_model_image_is_refused(self):
        proc = self.run_script("deploy_atomic.sh", "gke-cpu", self.digests("r.example/p/vllm-cpu-model:abc"),
                               self.dir / "ev", KUBE_CONTEXT=pf.EXPECTED_CONTEXT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse([c for c in self.calls() if c.startswith("helm upgrade")])


# --- G1 effective NetworkPolicy reachability (Kubernetes semantics, offline) ---

def g1_documents(*overrides):
    args = [HELM, "template", "x", str(CHART), "-f", str(CHART / "values-gke-cpu.yaml"),
            "-f", str(CHART / "values-gke-cpu-g1.yaml"), "--set", f"vllm.image={MODEL_IMAGE}"]
    for item in overrides:
        args += ["--set", item]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


NS_LABELS = {"kubernetes.io/metadata.name": "ewallet-agents", "istio.io/dataplane-mode": "ambient"}


def pod(labels, namespace="ewallet-agents"):
    ns_labels = NS_LABELS if namespace == "ewallet-agents" else {"kubernetes.io/metadata.name": namespace}
    return {"namespace": namespace, "labels": labels, "namespace_labels": ns_labels}


GATEWAY = pod({"app.kubernetes.io/name": "agentgateway"})
VLLM = pod({"app.kubernetes.io/name": "vllm", "app.kubernetes.io/instance": "primary"})
INTRUDER = pod({"g1-test": "unlabelled"})
OTHER_NS = pod({"app.kubernetes.io/name": "agentgateway"}, namespace="kubeflow")
KUBE_DNS = pod({"k8s-app": "kube-dns"}, namespace="kube-system")
PROBE = {"ip": "169.254.7.127"}
INTERNET = {"ip": "18.244.0.1"}
METADATA = {"ip": "169.254.169.254"}
GKE_METADATA = {"ip": "169.254.169.252"}
NODELOCAL = {"ip": "169.254.20.10"}


@unittest.skipUnless(HELM, "helm binary unavailable")
class G1ReachabilityTests(unittest.TestCase):
    """15008 and the probe/DNS openings authorize nothing beyond the declared flow."""

    @classmethod
    def setUpClass(cls):
        cls.docs = g1_documents()

    def allows(self, src, dst, port, docs=None):
        from ops.llm_agent.policy_lint import allows
        return allows(docs or self.docs, src, dst, port, namespace="ewallet-agents")

    def test_only_the_gateway_reaches_vllm_and_only_on_8000_and_hbone(self):
        endpoints = {"gateway": GATEWAY, "vllm": VLLM, "intruder": INTRUDER, "other-ns": OTHER_NS}
        ports = (8000, 4000, 3000, 15008, 15020, 8080, 9090)
        allowed = {(s, d, p) for s, src in endpoints.items() for d, dst in endpoints.items() if s != d
                   for p in ports if self.allows(src, dst, p)}
        # other-ns is outside the namespace: its own (absent) policies do not isolate it,
        # but no rule in ewallet-agents admits it.
        self.assertEqual(allowed, {("gateway", "vllm", 8000), ("gateway", "vllm", 15008)})

    def test_kubelet_probe_address_reaches_only_probe_ports(self):
        self.assertTrue(self.allows(PROBE, VLLM, 8000))
        self.assertTrue(self.allows(PROBE, GATEWAY, 4000))
        for dst, port in ((VLLM, 15008), (VLLM, 15020), (GATEWAY, 3000), (GATEWAY, 15008), (INTRUDER, 80)):
            self.assertFalse(self.allows(PROBE, dst, port), (dst, port))
        self.assertFalse(self.allows({"ip": "10.216.0.9"}, VLLM, 8000))

    def test_dns_allowed_and_everything_else_outbound_denied(self):
        for src in (GATEWAY, VLLM, INTRUDER):
            self.assertTrue(self.allows(src, KUBE_DNS, 53))
            self.assertTrue(self.allows(src, NODELOCAL, 53))
            for dst, port in ((INTERNET, 443), (METADATA, 80), (GKE_METADATA, 988), (KUBE_DNS, 443)):
                self.assertFalse(self.allows(src, dst, port), (src["labels"], dst, port))

    def test_metadata_access_is_opt_in_per_workload(self):
        docs = g1_documents("networkPolicy.metadataAccess={agentgateway}")
        self.assertTrue(self.allows(GATEWAY, METADATA, 80, docs))
        self.assertTrue(self.allows(GATEWAY, GKE_METADATA, 988, docs))
        self.assertFalse(self.allows(GATEWAY, METADATA, 443, docs))
        self.assertFalse(self.allows(VLLM, METADATA, 80, docs))

    def test_g1_overlay_has_no_namespace_or_kagent_objects(self):
        kinds = {d["kind"] for d in self.docs}
        self.assertNotIn("Namespace", kinds)
        self.assertNotIn("ModelConfig", kinds)
        self.assertFalse([c for c in pf.stage_crds("g1") if "kagent" in c])


# --- g1_operator.sh gates and failure injection (fake gcloud/kubectl/helm) ---

OPERATOR_FAKE = r"""#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$FAKE_LOG"
case "$(basename "$0") $*" in
  "gcloud compute instance-groups"*) cat "$FAKE_POOL_FILE" 2>/dev/null || echo 0;;
  "gcloud compute instances list"*) n=$(cat "$FAKE_POOL_FILE" 2>/dev/null || echo 0); for i in $(seq 1 "$n"); do echo "node-$i"; done;;
  "gcloud container clusters resize"*) n=$(echo "$*" | sed -n 's/.*--num-nodes \([0-9]*\).*/\1/p'); [ "${FAKE_RESIZE_STUCK:-0}" = 1 ] || echo "$n" > "$FAKE_POOL_FILE";;
  *"describe --format=value(networkPolicy.enabled"*) printf '%s\t%s\n' "${FAKE_NP:-True}" "False";;
  *"describe --format=value(endpoint"*) printf '34.0.0.1\tgke-x.asia-southeast1-b.gke.goog\n';;
  "kubectl config view"*) printf '%s' "${FAKE_SERVER:-https://gke-x.asia-southeast1-b.gke.goog}";;
  "kubectl"*" wait "*) [ -n "${FAKE_HANG:-}" ] && sleep 60;;
  "kubectl"*"get nodes -l"*"--no-headers"*) for i in $(seq 1 "${FAKE_READY:-3}"); do echo "n$i   Ready   <none>"; done;;
  "kubectl"*"get secret vllm-api-key -o jsonpath"*) printf '%s' "$(printf 'k%.0s' $(seq 1 64) | base64 -w0)";;
  "kubectl"*"get secret vllm-api-key"*) exit "${FAKE_SECRET_EXISTS:-1}";;
  "kubectl"*"get deploy vllm-fallback"*) printf '%s' "${FAKE_FALLBACK_READY:-0}";;
  "kubectl"*"get ns ewallet-agents"*) printf 'ambient';;
esac
exit 0
"""


class OperatorTests(ScriptHarness):
    def setUp(self):
        super().setUp()
        self.pool = self.dir / "pool"
        self.pool.write_text("0")
        for name in ("gcloud", "kubectl", "helm", "openssl"):
            path = self.bin / name
            path.write_text(OPERATOR_FAKE if name != "openssl" else "#!/usr/bin/env bash\nprintf 'secret-token-value'\n")
            path.chmod(0o755)
        self.base_env = {"KUBE_CONTEXT": pf.EXPECTED_CONTEXT, "FAKE_POOL_FILE": str(self.pool)}

    def op(self, stage, *extra, **env):
        return self.run_script("g1_operator.sh", stage, self.dir / "ev", *extra, **{**self.base_env, **env})

    def resizes(self):
        return [c for c in self.calls() if c.startswith("gcloud container clusters resize")]

    def cloud_mutations(self):
        return [c for c in self.calls() if " update " in f" {c} " or " resize " in f" {c} " or "create secret" in c
                or c.startswith("kubectl") and " run " in f" {c} "]

    # authorization
    def test_refuses_any_other_context_or_cluster(self):
        for env in ({"KUBE_CONTEXT": ""}, {"KUBE_CONTEXT": "kind-llm-agent-local"},
                    {"FAKE_SERVER": "https://10.9.9.9"}):
            with self.subTest(env):
                self.assertNotEqual(self.op("status", **env).returncode, 0)
        self.assertEqual(self.cloud_mutations(), [])

    def test_approval_list_is_exact_and_typos_fail(self):
        for approvals in ("scale_up", "scale-up,cleanup", "everything", "status"):
            with self.subTest(approvals):
                proc = self.op("scale-up", G1_APPROVED_STAGES=approvals)
                self.assertNotEqual(proc.returncode, 0)
        for stage in ("netpol-enable", "scale-up", "istio", "image-push", "namespace", "secret", "deploy",
                      "netpol-verify", "failover"):
            with self.subTest(stage):
                proc = self.op(stage, G1_APPROVED_STAGES="secret" if stage != "secret" else "istio")
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("G1_APPROVED_STAGES", proc.stderr)
        self.assertEqual(self.cloud_mutations(), [])

    def test_a_mutating_stage_runs_once_unless_a_rerun_is_named(self):
        self.assertEqual(self.op("netpol-enable", G1_APPROVED_STAGES="netpol-enable").returncode, 0)
        proc = self.op("netpol-enable", G1_APPROVED_STAGES="netpol-enable")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("G1_RERUN", proc.stderr)
        self.assertEqual(len([c for c in self.calls() if " update " in f" {c} "]), 2)

    # transitions and readbacks
    def test_netpol_enable_only_on_an_empty_pool_and_reads_back(self):
        self.pool.write_text("2")
        self.assertNotEqual(self.op("netpol-enable", G1_APPROVED_STAGES="netpol-enable").returncode, 0)
        self.pool.write_text("0")
        proc = self.op("netpol-enable", G1_APPROVED_STAGES="netpol-enable", G1_RERUN="netpol-enable", FAKE_NP="False")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("readback", proc.stderr)

    def test_scale_up_is_bounded_needs_enforcement_and_reads_back_exactly(self):
        self.assertNotEqual(self.op("scale-up", G1_APPROVED_STAGES="scale-up", NODES="4").returncode, 0)
        self.assertNotEqual(self.op("scale-up", G1_APPROVED_STAGES="scale-up", G1_RERUN="scale-up",
                                    FAKE_NP="False").returncode, 0)
        self.assertEqual(self.resizes(), [])
        proc = self.op("scale-up", G1_APPROVED_STAGES="scale-up", G1_RERUN="scale-up", FAKE_READY="2")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("expected exactly 3", proc.stderr)

    def test_failover_refuses_without_a_ready_fallback(self):
        proc = self.op("failover", G1_APPROVED_STAGES="failover", FAKE_FALLBACK_READY="0")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no Ready vllm-fallback", proc.stderr)
        self.assertFalse([c for c in self.calls() if "scale deploy/vllm-primary" in c])

    def test_secret_is_fresh_never_on_a_command_line_and_never_overwritten(self):
        proc = self.op("secret", G1_APPROVED_STAGES="secret")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = self.log.read_text()
        self.assertIn("create secret generic vllm-api-key --from-file=api-key=", log)
        self.assertNotIn("secret-token-value", log + proc.stdout + proc.stderr)
        self.assertNotIn("from-literal", log)
        before = log.count("create secret")
        proc = self.op("secret", G1_APPROVED_STAGES="secret", G1_RERUN="secret", FAKE_SECRET_EXISTS="0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.log.read_text().count("create secret"), before)

    # cleanup
    def test_cleanup_needs_no_approval_or_working_kubeconfig_and_reads_back_zero(self):
        self.pool.write_text("3")
        proc = self.op("cleanup", FAKE_SERVER="https://10.9.9.9")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--num-nodes 0", self.resizes()[-1])
        self.pool.write_text("3")
        self.assertNotEqual(self.op("cleanup", FAKE_RESIZE_STUCK="1").returncode, 0)

    def session_env(self, **env):
        approvals = "scale-up,istio,namespace,secret,deploy,netpol-verify,failover"
        return {"G1_APPROVED_STAGES": approvals, "DIGESTS": str(self.dir / "d.json"), **env}

    def test_session_needs_every_approval_and_enforcement_first(self):
        self.assertNotEqual(self.op("session", **self.session_env(G1_APPROVED_STAGES="scale-up")).returncode, 0)
        self.assertNotEqual(self.op("session", **self.session_env(FAKE_NP="False")).returncode, 0)
        self.assertNotEqual(self.op("session", **self.session_env(G1_SESSION_MINUTES="600")).returncode, 0)
        self.assertEqual(self.resizes(), [])

    def test_session_failure_scales_to_zero(self):
        proc = self.op("session", **self.session_env(FAKE_READY="2"))
        self.assertNotEqual(proc.returncode, 0)
        resizes = self.resizes()
        self.assertIn("--num-nodes 3", resizes[0])
        self.assertIn("--num-nodes 0", resizes[-1])
        self.assertEqual(self.pool.read_text().strip(), "0")
        self.assertIn("cleanup", (self.dir / "ev" / "operator.log").read_text())

    def test_session_interruption_scales_to_zero(self):
        import signal
        import time
        proc = subprocess.Popen([str(ROOT / "scripts/llm_agent/g1_operator.sh"), "session", str(self.dir / "ev")],
                                env={**self.env, **self.base_env, **self.session_env(FAKE_HANG="1")},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and "wait --for=condition=Ready" not in (self.log.read_text() if self.log.exists() else ""):
            time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        started = time.monotonic()
        proc.communicate(timeout=30)
        self.assertLess(time.monotonic() - started, 20, "the trap must not wait for the hung stage")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--num-nodes 0", self.resizes()[-1])
        self.assertEqual(self.pool.read_text().strip(), "0")


class BenchmarkCliTests(unittest.TestCase):
    def test_bounds_and_local_only(self):
        script = ROOT / "scripts/llm_agent/g1_benchmark.py"
        for args in (["--endpoint", "http://10.0.0.1:3000"], ["--requests", "100"], ["--concurrency", "9"]):
            base = {"--endpoint": "http://127.0.0.1:1", "--model": "m", "--resources": "r", "--output": "o"}
            argv = [str(script)]
            merged = {**base, **dict(zip(args[::2], args[1::2]))}
            for key, value in merged.items():
                argv += [key, value]
            proc = subprocess.run(["python3", *argv], capture_output=True, text=True,
                                  env={**os.environ, "EWALLET_VLLM_API_KEY": "x"})
            self.assertEqual(proc.returncode, 2, (args, proc.stderr))
