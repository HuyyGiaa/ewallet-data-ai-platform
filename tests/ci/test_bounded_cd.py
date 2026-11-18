"""Contract, safety and rollback tests for the bounded CD driver."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.ci.bounded_cd import (
    ApiKube,
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


ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
IMAGE = "registry.example/ewallet/x@sha256:" + "b" * 64
OLD_IMAGE = "registry.example/ewallet/x@sha256:" + "c" * 64
CONTRACTS = ("mcp-servers", "agent-fleet", "coordinator", "streaming")
WORKLOADS = ("mcp-cd-feature-rag", "mcp-cd-drift", "fleet-cd-feature-rag-specialist", "fleet-cd-drift-specialist",
             "coordinator-cd")
STORES = {"coordinator-cd-redis"}  # ephemeral stores run their own pinned image


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
            for name in WORKLOADS:  # workloads under test
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
            value = json.loads((ROOT / "jenkins/cd/coordinator.contract.json").read_text())
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
                if item["kind"] in {"Deployment", "Job"} and item["metadata"]["name"] not in STORES:
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
        text = (ROOT / contract("mcp-servers").manifest).read_text()
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
        kube = FakeKube(passing_log("mcp-servers"))
        verdict = self.run_it("mcp-servers", kube)
        self.assertEqual("PASS", verdict["verdict"])
        self.assertIn(("apply", "Deployment", "mcp-cd-drift", 0), kube.calls)
        self.assertIn(("remove", "ConfigMap", "mcp-cd-auth-drift"), kube.calls)
        self.assertIn(("remove", "Job", "mcp-cd-smoke"), kube.calls)
        self.assertNotIn(("ConfigMap", "mcp-cd-auth-drift"), kube.objects)
        self.assertNotIn(("ConfigMap", "mcp-cd-auth-feature-rag"), kube.objects)
        self.assertIn(("apply", "Deployment", "mcp-cd-feature-rag", 0), kube.calls)

    def test_the_verdict_records_the_rollback_target(self):
        kube = FakeKube(passing_log("mcp-servers"), previous_image=OLD_IMAGE)
        verdict = self.run_it("mcp-servers", kube)
        self.assertEqual({"mcp-cd-feature-rag": OLD_IMAGE, "mcp-cd-drift": OLD_IMAGE}, verdict["previous_images"])
        self.assertEqual("rehearsal", verdict["environment"])
        self.assertFalse(verdict["production_changed"])
        self.assertTrue((self.evidence / "verdict.json").is_file())

    def test_a_job_only_release_works(self):
        self.assertEqual("PASS", self.run_it("streaming", FakeKube(passing_log("streaming")))["verdict"])

    def test_a_failed_job_restores_the_previous_workload(self):
        kube = FakeKube(passing_log("mcp-servers"), previous_image=OLD_IMAGE, job_passes=False)
        with self.assertRaises(ReleaseError):
            self.run_it("mcp-servers", kube)
        self.assertEqual(OLD_IMAGE, deployment_image(kube.objects[("Deployment", "mcp-cd-drift")]))
        self.assertEqual(0, kube.objects[("Deployment", "mcp-cd-drift")]["spec"]["replicas"])
        self.assertEqual("FAIL", json.loads((self.evidence / "verdict.json").read_text())["verdict"])

    def test_a_failed_first_release_removes_the_workload(self):
        kube = FakeKube(passing_log("mcp-servers"), job_passes=False)
        with self.assertRaises(ReleaseError):
            self.run_it("mcp-servers", kube)
        self.assertNotIn(("Deployment", "mcp-cd-drift"), kube.objects)
        # Batch 2B-1: the first-release rollback left the two Services behind.
        self.assertNotIn(("Service", "mcp-cd-drift"), kube.objects)
        self.assertNotIn(("Service", "mcp-cd-feature-rag"), kube.objects)

    def test_a_failed_release_keeps_services_that_existed_before(self):
        kube = FakeKube(passing_log("mcp-servers"), previous_image=OLD_IMAGE, job_passes=False)
        kube.objects[("Service", "mcp-cd-drift")] = {"metadata": {"name": "mcp-cd-drift"}}
        with self.assertRaises(ReleaseError):
            self.run_it("mcp-servers", kube)
        self.assertIn(("Service", "mcp-cd-drift"), kube.objects)

    def test_the_smoke_job_starts_only_after_every_deployment_is_ready(self):
        """Batch 2B-1, edai2-cd-mcp-agents #1: applied with the Deployments, the Job hit servers that still
        refused connections (URLError) and failed with backoffLimit 0."""
        for name in ("mcp-servers", "agent-fleet", "coordinator"):
            with self.subTest(name=name):
                kube = FakeKube(passing_log(name))
                try:
                    self.run_it(name, kube)
                except ReleaseError:
                    pass
                job_apply = next(i for i, c in enumerate(kube.calls) if c[0] == "apply" and c[1] == "Job")
                waits = [i for i, c in enumerate(kube.calls) if c[0] == "wait"]
                self.assertTrue(waits and max(waits) < job_apply, kube.calls)

    def test_a_contract_mismatch_fails_even_when_the_job_succeeds(self):
        expected = contract("mcp-servers").expected
        log = "CD_SMOKE_SUMMARY " + json.dumps({**expected, "release_sha": SHA, "drift-mcp": {
            **expected["drift-mcp"], "tools_missing": ["drift_detection"]}}) + "\n"
        with self.assertRaises(ReleaseError):
            self.run_it("mcp-servers", FakeKube(log))

    def test_another_digest_in_the_cluster_fails(self):
        kube = FakeKube(passing_log("mcp-servers"), drift_image=OLD_IMAGE)
        with self.assertRaisesRegex(ReleaseError, "does not run"):
            self.run_it("mcp-servers", kube)

    def test_transients_are_removed_after_a_failure(self):
        kube = FakeKube("no summary")
        with self.assertRaises(ReleaseError):
            self.run_it("mcp-servers", kube)
        self.assertIn(("remove", "ConfigMap", "mcp-cd-auth-drift"), kube.calls)

    def registry_contract(self):
        value = json.loads((ROOT / "jenkins/cd/coordinator.contract.json").read_text())
        path = self.evidence / "registry.contract.json"
        path.write_text(json.dumps({**value, "requires_prompt_registry": True}))
        return Contract.load(path)

    def test_a_contract_requiring_the_prompt_registry_fails_without_it(self):
        kube = FakeKube(passing_log("coordinator"))
        with self.assertRaisesRegex(ReleaseError, "prompt registry"):
            run_release(self.registry_contract(), kube, rendered("coordinator"), image=IMAGE, git_sha=SHA,
                        evidence=self.evidence, prompt_registry_ok=False)
        self.assertNotIn("wait", [call[0] for call in kube.calls])

    def test_coordinator_rehearsal_parks_and_removes_its_store(self):
        kube = FakeKube(passing_log("coordinator"))
        verdict = self.run_it("coordinator", kube)
        self.assertEqual("PASS", verdict["verdict"])
        self.assertNotIn(("Deployment", "coordinator-cd-redis"), kube.objects)
        self.assertNotIn(("Service", "coordinator-cd-redis"), kube.objects)
        self.assertNotIn(("ConfigMap", "coordinator-cd-env"), kube.objects)
        self.assertEqual(0, kube.objects[("Deployment", "coordinator-cd")]["spec"]["replicas"])

    def test_a_parked_workload_keeps_no_token(self):
        kube = FakeKube(passing_log("coordinator"))
        run_release(contract("coordinator"), kube, rendered("coordinator", "s3cret-token"), image=IMAGE, git_sha=SHA,
                    evidence=self.evidence)
        self.assertNotIn("s3cret-token", str(kube.objects))

    def test_fleet_rehearsal_covers_both_specialists(self):
        kube = FakeKube(passing_log("agent-fleet"))
        self.assertEqual("PASS", self.run_it("agent-fleet", kube)["verdict"])
        for name in ("fleet-cd-feature-rag-specialist", "fleet-cd-drift-specialist"):
            self.assertIn(("wait", name), kube.calls)
            self.assertEqual(0, kube.objects[("Deployment", name)]["spec"]["replicas"])

    def test_one_specialist_failing_fails_the_fleet(self):
        expected = contract("agent-fleet").expected
        log = "CD_SMOKE_SUMMARY " + json.dumps({**expected, "release_sha": SHA, "drift_specialist": {
            **expected["drift_specialist"], "card_status": 503}}) + "\n"
        with self.assertRaisesRegex(ReleaseError, "drift_specialist"):
            self.run_it("agent-fleet", FakeKube(log))

    def test_a_deployment_that_never_becomes_ready_fails_the_rehearsal(self):
        class NeverReady(FakeKube):
            def wait_deployment(self, name, timeout):
                raise ReleaseError(f"Deployment {name} did not become ready within 300 s")

        kube = NeverReady(passing_log("mcp-servers"), previous_image=OLD_IMAGE)
        with self.assertRaisesRegex(ReleaseError, "did not become ready"):
            self.run_it("mcp-servers", kube)
        self.assertEqual(OLD_IMAGE, deployment_image(kube.objects[("Deployment", "mcp-cd-drift")]))

    def test_a_token_never_reaches_the_verdict(self):
        kube = FakeKube(passing_log("mcp-servers"))
        run_release(contract("mcp-servers"), kube, rendered("mcp-servers", "s3cret-token"), image=IMAGE, git_sha=SHA,
                    evidence=self.evidence)
        self.assertNotIn("s3cret-token", (self.evidence / "verdict.json").read_text())


class ApiKubeTest(unittest.TestCase):
    def test_wait_ready_timeout_is_an_error_not_a_pass(self):
        """``wait_ready`` reports a timeout by returning False; the driver used to ignore it."""

        class Client:
            def request(self, method, path, body=None, content_type="", raw=False):
                return {"metadata": {"generation": 2}, "spec": {"replicas": 1},
                        "status": {"observedGeneration": 1, "readyReplicas": 0}}

        with self.assertRaisesRegex(ReleaseError, "did not become ready"):
            ApiKube(Client(), CD_NAMESPACE).wait_deployment("x", 0)


class SeedingTest(unittest.TestCase):
    def test_jenkins_rbac_is_not_broadened(self):
        bindings = subprocess.run(["git", "grep", "-l", "kind: ClusterRoleBinding", "--", "infra/kubernetes/jenkins"],
                                  cwd=ROOT, capture_output=True, text=True).stdout.split()
        for path in bindings:
            self.assertIn("PROPOSAL", "\n".join((ROOT / path).read_text().splitlines()[:3]))

    def test_every_release_group_has_one_seeded_job(self):
        text = (ROOT / "infra/kubernetes/jenkins/jenkins.yaml").read_text()
        groups = json.loads((ROOT / "jenkins/catalog/release-units.json").read_text())["groups"]
        for branch in groups:
            self.assertEqual(1, text.count(f"'*/{branch}'"), branch)
        self.assertNotIn("'*/release/agents'", text)


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
