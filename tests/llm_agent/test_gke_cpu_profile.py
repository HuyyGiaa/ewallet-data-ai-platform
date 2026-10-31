"""GKE CPU runtime profile: overlay, Istio ambient install script, stage preflight.

All offline: the preflight decision runs on fixture facts, the Istio script on
fake helm/kubectl/curl and locally built chart files.
"""

from __future__ import annotations

import hashlib
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
        self.assertIn(f"FROM {base}\n", dockerfile)
        local = yaml.safe_load((CHART / "values-local.yaml").read_text())["vllm"]["modelSource"]["staged"]["sha256sums"]
        self.assertEqual((ROOT / "infra/docker/vllm-cpu-model/SHA256SUMS").read_text(), local)


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
        (self.repo / "infra/kubernetes/istio-ambient/versions.env").write_text(
            "ISTIO_VERSION=1.30.7\nISTIO_CHART_REPO=https://invalid.example\n" + "\n".join(lines) + "\n")

    def run_istio(self, mode, **env):
        return subprocess.run([str(self.repo / "scripts/llm_agent/istio_ambient_gke.sh"), mode, str(self.dir / "ev")],
                              capture_output=True, text=True, check=False,
                              env={**self.env, "ISTIO_CHART_DIR": str(self.charts), **env})

    def test_plan_renders_and_installs_nothing(self):
        proc = self.run_istio("plan", KUBE_CONTEXT=pf.EXPECTED_CONTEXT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len([c for c in self.calls() if c.startswith("helm template")]), 4)
        self.assertFalse([c for c in self.calls() if c.startswith("helm upgrade")])

    def test_apply_installs_in_order_with_explicit_context(self):
        proc = self.run_istio("apply", KUBE_CONTEXT=pf.EXPECTED_CONTEXT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        upgrades = [c for c in self.calls() if c.startswith("helm upgrade")]
        self.assertEqual([c.split()[3] for c in upgrades], ["istio-base", "istiod", "istio-cni", "ztunnel"])
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
        self.assertFalse([c for c in self.calls() if c.startswith("helm upgrade")])


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
