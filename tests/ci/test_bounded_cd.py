"""Contract, safety and rollback tests for the bounded CD driver."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.ci.bounded_cd import (
    CD_NAMESPACE,
    Contract,
    ReleaseError,
    deployment_image,
    parse_summary,
    render,
    require_namespace,
    run_release,
    verify_summary,
)
from scripts.ci.catalog import load_catalog
from scripts.ci.release_policy import ReleasePolicyError, ReleaseRequest, build_release_plan


ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
IMAGE = "registry.example/ewallet/x@sha256:" + "b" * 64
OLD_IMAGE = "registry.example/ewallet/x@sha256:" + "c" * 64
CONTRACTS = ("mcp-agents", "agents", "streaming")


def contract(name: str) -> Contract:
    return Contract.load(ROOT / f"jenkins/cd/{name}.contract.json")


def rendered(name: str, token: str = "t0k3n") -> list[dict]:
    item = contract(name)
    return render((ROOT / item.manifest).read_text(), image=IMAGE, git_sha=SHA, token=token, smoke_source="print()")


def passing_log(name: str) -> str:
    item = contract(name)
    return "CD_SMOKE_SUMMARY " + json.dumps({**item.expected, "release_sha": SHA}) + "\n"


class FakeKube:
    """Records calls and simulates one namespace."""

    def __init__(self, log: str, *, previous_image: str | None = None, job_passes: bool = True,
                 drift_image: str | None = None) -> None:
        self.log, self.job_passes, self.drift_image = log, job_passes, drift_image
        self.objects: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple] = []
        if previous_image:
            for name in ("mcp-cd-drift", "agent-cd-coordinator", "agent-cd-specialist"):  # workloads under test
                self.objects[("Deployment", name)] = {
                    "metadata": {"name": name, "namespace": CD_NAMESPACE},
                    "spec": {"replicas": 0, "template": {"spec": {"containers": [{"image": previous_image}]}}},
                }

    def apply_documents(self, documents):
        for item in documents:
            self.calls.append(("apply", item["kind"], item["metadata"]["name"], item.get("spec", {}).get("replicas")))
            self.objects[(item["kind"], item["metadata"]["name"])] = item
        return []

    def read(self, kind, name):
        item = self.objects.get((kind, name))
        if item and self.drift_image and kind == "Deployment":
            return {**item, "spec": {"template": {"spec": {"containers": [{"image": self.drift_image}]}}}}
        return item

    def remove(self, kind, name):
        self.calls.append(("remove", kind, name))
        self.objects.pop((kind, name), None)

    def wait_deployment(self, name, timeout):
        self.calls.append(("wait", name))

    def wait_for_job(self, name, timeout):
        return self.job_passes

    def logs(self, job):
        return self.log


class ReleaseCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.evidence = Path(directory.name)

    def run_it(self, name, kube, **options):
        return run_release(contract(name), kube, rendered(name), image=IMAGE, git_sha=SHA,
                           evidence=self.evidence, **options)


class ContractTest(unittest.TestCase):
    def test_every_contract_targets_the_isolated_namespace(self):
        for name in CONTRACTS:
            with self.subTest(name):
                self.assertEqual(CD_NAMESPACE, contract(name).namespace)
                require_namespace(rendered(name), CD_NAMESPACE)

    def test_a_contract_for_another_namespace_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.json"
            value = json.loads((ROOT / "jenkins/cd/agents.contract.json").read_text())
            path.write_text(json.dumps({**value, "namespace": "ewallet-agents"}))
            with self.assertRaisesRegex(ReleaseError, "targets ewallet-agents"):
                Contract.load(path)

    def test_an_incomplete_contract_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.json"
            path.write_text('{"component": "x"}')
            with self.assertRaisesRegex(ReleaseError, "missing"):
                Contract.load(path)

    def test_manifests_use_only_kinds_jenkins_may_write(self):
        allowed = {"ConfigMap", "Service", "Deployment", "Job"}
        for name in CONTRACTS:
            with self.subTest(name):
                self.assertLessEqual({item["kind"] for item in rendered(name)}, allowed)

    def test_workloads_meet_the_restricted_pod_security_profile(self):
        for name in CONTRACTS:
            for item in rendered(name):
                if item["kind"] not in {"Deployment", "Job"}:
                    continue
                pod = item["spec"]["template"]["spec"]
                with self.subTest(item["metadata"]["name"]):
                    self.assertTrue(pod["securityContext"]["runAsNonRoot"])
                    self.assertFalse(pod["automountServiceAccountToken"])
                    for container in pod["containers"]:
                        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
                        self.assertEqual(["ALL"], container["securityContext"]["capabilities"]["drop"])
                        self.assertIn("requests", container["resources"])

    def test_every_workload_runs_the_released_digest(self):
        for name in CONTRACTS:
            for item in rendered(name):
                if item["kind"] in {"Deployment", "Job"} and item["metadata"]["name"] != "agent-cd-redis":
                    self.assertEqual(IMAGE, deployment_image(item))

    def test_requests_fit_the_namespace_quota(self):
        def cpu(value):
            return float(value[:-1]) / 1000 if value.endswith("m") else float(value)

        for name in CONTRACTS:
            total = sum(
                cpu(container["resources"]["requests"]["cpu"])
                for item in rendered(name) if item["kind"] in {"Deployment", "Job"}
                for container in item["spec"]["template"]["spec"]["containers"]
            )
            self.assertLessEqual(total, 2.0)

    def test_no_source_file_holds_a_token(self):
        text = (ROOT / contract("mcp-agents").manifest).read_text()
        self.assertIn("__SMOKE_TOKEN__", text)
        self.assertNotIn('"token":"', text.replace('"token":"__SMOKE_TOKEN__"', ""))


class RenderTest(unittest.TestCase):
    def test_an_unresolved_placeholder_stops_the_release(self):
        with self.assertRaisesRegex(ReleaseError, "unresolved"):
            render("metadata: {name: __MISSING__}", image=IMAGE, git_sha=SHA, token="", smoke_source="")

    def test_the_smoke_script_is_injected(self):
        script = next(item for item in rendered("streaming") if item["kind"] == "ConfigMap")
        self.assertEqual("print()", script["data"]["cd_smoke.py"])

    def test_a_document_in_a_production_namespace_is_refused(self):
        documents = rendered("streaming")
        documents[0]["metadata"]["namespace"] = "ewallet-agents"
        with self.assertRaisesRegex(ReleaseError, "ewallet-agents"):
            require_namespace(documents, CD_NAMESPACE)


class SummaryTest(unittest.TestCase):
    def test_the_last_summary_wins(self):
        log = 'CD_SMOKE_SUMMARY {"a": 1}\nnoise\nCD_SMOKE_SUMMARY {"a": 2}\n'
        self.assertEqual({"a": 2}, parse_summary(log))

    def test_a_missing_or_broken_summary_is_an_error(self):
        for log in ("no summary", "CD_SMOKE_SUMMARY {broken"):
            with self.subTest(log), self.assertRaises(ReleaseError):
                parse_summary(log)

    def test_a_summary_from_another_release_fails(self):
        self.assertTrue(verify_summary({"release_sha": "f" * 40}, {}, SHA))

    def test_a_wrong_value_fails(self):
        self.assertTrue(verify_summary({"release_sha": SHA, "tool_found": False}, {"tool_found": True}, SHA))


class ReleaseFlowTest(ReleaseCase):
    def test_a_passing_release_parks_the_workload_and_removes_transients(self):
        kube = FakeKube(passing_log("mcp-agents"))
        verdict = self.run_it("mcp-agents", kube)
        self.assertEqual("PASS", verdict["verdict"])
        self.assertIn(("apply", "Deployment", "mcp-cd-drift", 0), kube.calls)
        self.assertIn(("remove", "ConfigMap", "mcp-cd-auth"), kube.calls)
        self.assertIn(("remove", "Job", "mcp-cd-smoke"), kube.calls)
        self.assertNotIn(("ConfigMap", "mcp-cd-auth"), kube.objects)

    def test_the_verdict_records_the_rollback_target(self):
        kube = FakeKube(passing_log("mcp-agents"), previous_image=OLD_IMAGE)
        verdict = self.run_it("mcp-agents", kube)
        self.assertEqual({"mcp-cd-drift": OLD_IMAGE}, verdict["previous_images"])
        self.assertTrue((self.evidence / "verdict.json").is_file())

    def test_a_job_only_release_works(self):
        self.assertEqual("PASS", self.run_it("streaming", FakeKube(passing_log("streaming")))["verdict"])

    def test_a_failed_job_restores_the_previous_workload(self):
        kube = FakeKube(passing_log("mcp-agents"), previous_image=OLD_IMAGE, job_passes=False)
        with self.assertRaises(ReleaseError):
            self.run_it("mcp-agents", kube)
        self.assertEqual(OLD_IMAGE, deployment_image(kube.objects[("Deployment", "mcp-cd-drift")]))
        self.assertEqual(0, kube.objects[("Deployment", "mcp-cd-drift")]["spec"]["replicas"])
        self.assertEqual("FAIL", json.loads((self.evidence / "verdict.json").read_text())["verdict"])

    def test_a_failed_first_release_removes_the_workload(self):
        kube = FakeKube(passing_log("mcp-agents"), job_passes=False)
        with self.assertRaises(ReleaseError):
            self.run_it("mcp-agents", kube)
        self.assertNotIn(("Deployment", "mcp-cd-drift"), kube.objects)

    def test_a_contract_mismatch_fails_even_when_the_job_succeeds(self):
        log = 'CD_SMOKE_SUMMARY {"release_sha": "%s", "tool_found": false}\n' % SHA
        with self.assertRaises(ReleaseError):
            self.run_it("mcp-agents", FakeKube(log))

    def test_another_digest_in_the_cluster_fails(self):
        kube = FakeKube(passing_log("mcp-agents"), drift_image=OLD_IMAGE)
        with self.assertRaisesRegex(ReleaseError, "does not run"):
            self.run_it("mcp-agents", kube)

    def test_transients_are_removed_after_a_failure(self):
        kube = FakeKube("no summary")
        with self.assertRaises(ReleaseError):
            self.run_it("mcp-agents", kube)
        self.assertIn(("remove", "ConfigMap", "mcp-cd-auth"), kube.calls)

    def test_agents_need_a_verified_prompt_registry(self):
        kube = FakeKube(passing_log("agents"))
        with self.assertRaisesRegex(ReleaseError, "prompt registry"):
            self.run_it("agents", kube, prompt_registry_ok=False)
        self.assertNotIn("wait", [call[0] for call in kube.calls])

    def test_agents_pass_with_a_verified_prompt_registry(self):
        kube = FakeKube(passing_log("agents"))
        verdict = self.run_it("agents", kube, prompt_registry_ok=True)
        self.assertTrue(verdict["prompt_registry_verified"])
        self.assertNotIn(("Deployment", "agent-cd-redis"), kube.objects)
        self.assertNotIn(("Service", "agent-cd-redis"), kube.objects)
        self.assertNotIn(("ConfigMap", "agent-cd-env"), kube.objects)
        self.assertEqual(0, kube.objects[("Deployment", "agent-cd-coordinator")]["spec"]["replicas"])

    def test_a_parked_workload_keeps_no_token(self):
        kube = FakeKube(passing_log("agents"))
        run_release(contract("agents"), kube, rendered("agents", "s3cret-token"), image=IMAGE, git_sha=SHA,
                    evidence=self.evidence, prompt_registry_ok=True)
        self.assertNotIn("s3cret-token", str(kube.objects))

    def test_a_token_never_reaches_the_verdict(self):
        kube = FakeKube(passing_log("mcp-agents"))
        run_release(contract("mcp-agents"), kube, rendered("mcp-agents", "s3cret-token"), image=IMAGE, git_sha=SHA,
                    evidence=self.evidence)
        self.assertNotIn("s3cret-token", (self.evidence / "verdict.json").read_text())


class CatalogIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog(ROOT / "jenkins/catalog/components.json")

    def request(self, branch, components=("mcp-agents",), sha=SHA, checkout=SHA, approved=True):
        return ReleaseRequest(True, approved, "owner" if approved else "", branch, sha, checkout, "gke-cpu",
                              "in-cluster", components, "manifest.json", "evidence")

    def manifest(self, sha=SHA, image="mcp-tools", reference=IMAGE):
        return {"git_sha": sha, "images": {image: {"digest_ref": reference}}}

    def test_each_component_has_a_handler_contract_and_target(self):
        self.catalog.validate(ROOT)
        for name in CONTRACTS:
            component = self.catalog.components[name]
            self.assertTrue((ROOT / component.deploy_handler).is_file())
            self.assertTrue(component.release_artifact_selector.startswith("images/"))

    def test_a_handler_is_allowlisted_by_the_dispatcher(self):
        text = (ROOT / "scripts/ci/deploy_release.sh").read_text()
        for name in CONTRACTS:
            self.assertIn(self.catalog.components[name].deploy_handler, text)

    def test_a_release_branch_ships_only_its_component(self):
        plan = build_release_plan(self.catalog, self.request("release/mcp-agents", ("mcp-agents", "rag-pipeline")),
                                  self.manifest())
        self.assertEqual(["mcp-agents"], [unit["component"] for unit in plan["units"]])

    def test_a_sha_mismatch_is_refused(self):
        with self.assertRaisesRegex(ReleasePolicyError, "checkout SHA"):
            build_release_plan(self.catalog, self.request("release/mcp-agents", checkout="f" * 40), self.manifest())
        with self.assertRaisesRegex(ReleasePolicyError, "manifest git SHA"):
            build_release_plan(self.catalog, self.request("release/mcp-agents"), self.manifest(sha="f" * 40))

    def test_a_tag_instead_of_a_digest_is_refused(self):
        with self.assertRaisesRegex(ReleasePolicyError, "immutable digest"):
            build_release_plan(self.catalog, self.request("release/mcp-agents"),
                               self.manifest(reference="registry.example/x:latest"))

    def test_an_unapproved_release_is_refused(self):
        with self.assertRaisesRegex(ReleasePolicyError, "owner approval"):
            build_release_plan(self.catalog, self.request("release/mcp-agents", approved=False), self.manifest())

    def test_a_non_release_branch_is_refused(self):
        with self.assertRaisesRegex(ReleasePolicyError, "release/"):
            build_release_plan(self.catalog, self.request("feature/x"), self.manifest())

    def test_jenkins_rbac_is_not_broadened(self):
        bindings = subprocess.run(["git", "grep", "-l", "kind: ClusterRoleBinding", "--", "infra/kubernetes/jenkins"],
                                  cwd=ROOT, capture_output=True, text=True).stdout.split()
        for path in bindings:
            self.assertIn("PROPOSAL", "\n".join((ROOT / path).read_text().splitlines()[:3]))

    def test_the_agents_job_is_seeded_from_its_release_branch(self):
        text = (ROOT / "infra/kubernetes/jenkins/jenkins.yaml").read_text()
        self.assertIn("createPipeline('edai2-cd-agents', '*/release/agents'", text)


class SmokeManifestTest(unittest.TestCase):
    def test_smoke_jobs_exist_for_each_contract(self):
        for name in CONTRACTS:
            names = [item["metadata"]["name"] for item in rendered(name) if item["kind"] == "Job"]
            self.assertEqual([contract(name).smoke_job], names)

    def test_yaml_is_valid_before_rendering(self):
        for name in CONTRACTS:
            list(yaml.safe_load_all((ROOT / contract(name).manifest).read_text()))


if __name__ == "__main__":
    unittest.main()


class _Strategy:
    """Stands in for a PyFlink WatermarkStrategy."""

    def __init__(self, assigner: object | None) -> None:
        self._timestamp_assigner = assigner


def test_event_time_gate_passes_only_for_the_fixed_wiring() -> None:
    from scripts.ci.cd_smoke import event_time_verdict

    assert event_time_verdict(_Strategy(None), _Strategy(object())) is True


def test_event_time_gate_fails_the_old_wiring() -> None:
    from scripts.ci.cd_smoke import event_time_verdict

    # The assigner goes to from_source, which drops it, and nothing is applied afterwards.
    assert event_time_verdict(_Strategy(object()), None) is False


def test_event_time_gate_fails_when_nothing_assigns_event_time() -> None:
    from scripts.ci.cd_smoke import event_time_verdict

    assert event_time_verdict(_Strategy(None), None) is False
    assert event_time_verdict(_Strategy(None), _Strategy(None)) is False


def test_streaming_release_is_refused_without_the_event_time_gate() -> None:
    import json
    from pathlib import Path

    from scripts.ci.bounded_cd import verify_summary

    root = Path(__file__).resolve().parents[2]
    expected = json.loads((root / "jenkins/cd/streaming.contract.json").read_text(encoding="utf-8"))["expected"]
    assert expected["event_time_after_source"] is True
    good = {**expected, "release_sha": "s"}
    assert verify_summary(good, expected, "s") == []
    for summary in ({**good, "event_time_after_source": False}, {k: v for k, v in good.items() if k != "event_time_after_source"}):
        assert any("event_time_after_source" in item for item in verify_summary(summary, expected, "s"))
