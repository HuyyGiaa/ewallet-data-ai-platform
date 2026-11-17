"""Release planning from typed release units: gates, ordering, artifacts and stale plans."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.ci.catalog import load_catalog
from scripts.ci.release_policy import (
    ReleasePolicyError, ReleaseRequest, build_release_plan, manifest_sha256, required_images, select_units,
    verify_release_plan,
)
from scripts.ci.release_units import ReleaseUnitError, load_release_units


ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "jenkins/catalog/components.json")
UNITS = load_release_units(ROOT / "jenkins/catalog/release-units.json")
SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "e" * 64
IMAGES = ("rag-ingestion", "mcp-tools", "g3-byo-coordinator", "flink-block-a", "streaming-sinks")


def reference(name: str) -> str:
    return f"asia-southeast1-docker.pkg.dev/p/ewallet-platform/{name}@{DIGEST}"


def manifest(**changes: object) -> dict:
    data = {"git_sha": SHA, "images": {name: {"digest_ref": reference(name), "revision": SHA} for name in IMAGES}}
    data.update(changes)
    return data


class ReleasePolicyTests(unittest.TestCase):
    def request(self, **changes: object) -> ReleaseRequest:
        values = {
            "requested": True, "owner_approved": True, "owner_approval": "owner", "branch": "release/coordinator",
            "git_sha": SHA, "checkout_sha": SHA, "environment": "gke-cpu", "kube_context": "in-cluster",
            "components": ("agents", "mcp-agents"), "image_manifest_path": "evidence/image-manifest.json",
            "evidence_dir": "evidence/release",
        }
        values.update(changes)
        return ReleaseRequest(**values)

    def plan(self, request: ReleaseRequest | None = None, data: dict | None = None) -> dict:
        return build_release_plan(CATALOG, UNITS, request or self.request(), data or manifest(), "a" * 64)

    def test_catalog_and_release_units_are_valid(self) -> None:
        CATALOG.validate(ROOT)
        UNITS.validate(ROOT, CATALOG)

    def test_deployment_is_denied_by_default_and_needs_approval_branch_and_sha(self) -> None:
        cases = (
            (self.request(requested=False), "disabled"),
            (self.request(owner_approved=False), "owner approval"),
            (self.request(branch="codex/feature"), "release branch"),
            (self.request(checkout_sha="f" * 40), "checkout SHA"),
            (self.request(environment="production"), "environment"),
            (self.request(stage="staging"), "stage"),
        )
        for request, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ReleasePolicyError, message):
                self.plan(request)

    def test_unknown_release_branch_no_longer_keeps_the_whole_scope(self) -> None:
        # The removed behaviour let release/<anything> deploy every changed component, which
        # reached the production llm-agent handler (P0 CICD-P0-F01).
        with self.assertRaisesRegex(ReleasePolicyError, "has no release group"):
            self.plan(self.request(branch="release/2026-11-17", components=("llm-serving", "agents")))

    def test_coordinator_release_orders_mcp_then_fleet_then_coordinator(self) -> None:
        plan = self.plan()
        self.assertEqual(["mcp-servers", "agent-fleet", "coordinator"], [unit["name"] for unit in plan["units"]])
        self.assertEqual("rehearsal", plan["stage"])
        self.assertEqual({"g3-byo-coordinator": reference("g3-byo-coordinator")}, plan["units"][2]["images"])
        self.assertEqual({"mcp-tools": reference("mcp-tools")}, plan["units"][0]["images"])

    def test_mcp_agents_release_deploys_mcp_before_the_dependent_fleet(self) -> None:
        plan = self.plan(self.request(branch="release/mcp-agents", components=("mcp-agents",)))
        names = [unit["name"] for unit in plan["units"]]
        self.assertEqual(["prompt-registry-gate", "mcp-servers", "agent-fleet"], names)
        self.assertLess(names.index("mcp-servers"), names.index("agent-fleet"))

    def test_rag_release_takes_only_its_own_digest(self) -> None:
        plan = self.plan(self.request(branch="release/rag-pipeline", components=("rag-pipeline",)))
        self.assertEqual([("rag-pipeline", {"rag-ingestion": reference("rag-ingestion")})],
                         [(unit["name"], unit["images"]) for unit in plan["units"]])
        self.assertEqual("ewallet-cicd", plan["units"][0]["target_namespace"])

    def test_streaming_release_needs_both_images(self) -> None:
        request = self.request(branch="release/streaming", components=("streaming",))
        plan = self.plan(request)
        self.assertEqual({"flink-block-a", "streaming-sinks"}, set(plan["units"][0]["images"]))
        data = manifest()
        del data["images"]["streaming-sinks"]
        with self.assertRaisesRegex(ReleasePolicyError, "no immutable digest for \\['streaming-sinks'\\]"):
            self.plan(request, data)

    def test_required_images_drive_the_build(self) -> None:
        names = select_units(UNITS, self.request(branch="release/streaming", components=("streaming",)), CATALOG)
        self.assertEqual(["flink-block-a", "streaming-sinks"], required_images(UNITS, names))
        names = select_units(UNITS, self.request(branch="release/prompt-management",
                                                 components=("prompt-management",)), CATALOG)
        self.assertEqual([], required_images(UNITS, names))

    def test_a_group_whose_components_did_not_change_is_refused(self) -> None:
        with self.assertRaisesRegex(ReleasePolicyError, "changes none of its components"):
            self.plan(self.request(branch="release/streaming", components=("rag-pipeline",)))

    def test_artifact_failures_are_refused(self) -> None:
        mutable = manifest()
        mutable["images"]["mcp-tools"]["digest_ref"] = "registry.example/mcp-tools:latest"
        other_revision = manifest()
        other_revision["images"]["g3-byo-coordinator"]["revision"] = "f" * 40
        missing = manifest()
        del missing["images"]["mcp-tools"]
        cases = ((mutable, "immutable digest"), (manifest(git_sha="f" * 40), "manifest git SHA"),
                 (other_revision, "not the release SHA"), (missing, "no immutable digest"))
        for data, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ReleasePolicyError, message):
                self.plan(data=data)

    def test_production_needs_a_distinct_approver_and_an_identity(self) -> None:
        base = dict(stage="production", triggered_by="alice", owner_approval="bob", production_identity="deployer")
        plan = self.plan(self.request(**base))
        # release/coordinator production is the full stack of one SHA: the candidate first (it fails before
        # any production mutation), then MCP, the fleet and the Coordinator.
        self.assertEqual(["agent-fleet-candidate", "mcp-servers-production", "agent-fleet-production",
                          "coordinator-production"], [unit["name"] for unit in plan["units"]])
        for changes, message in ((dict(owner_approval="alice"), "someone other"),
                                 (dict(triggered_by=""), "triggered"),
                                 (dict(production_identity=""), "deployer identity")):
            with self.subTest(message=message), self.assertRaisesRegex(ReleasePolicyError, message):
                self.plan(self.request(**{**base, **changes}))

    def test_production_fleet_release_runs_the_candidate_before_the_patch_promotion(self) -> None:
        request = self.request(branch="release/agent-fleet", stage="production", triggered_by="alice",
                               owner_approval="bob", production_identity="deployer")
        plan = self.plan(request)
        self.assertEqual(["agent-fleet-candidate", "agent-fleet-production"], [unit["name"] for unit in plan["units"]])
        self.assertEqual({"ewallet-sandbox"}, {unit["target_namespace"] for unit in plan["units"]})

    def test_a_blocked_unit_is_refused_not_silently_skipped(self) -> None:
        data = json.loads((ROOT / "jenkins/catalog/release-units.json").read_text())
        data["units"]["agent-fleet-production"].update(automation="blocked", blocked_reason="owner hold")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "units.json"
            path.write_text(json.dumps(data))
            units = load_release_units(path)
        request = self.request(branch="release/agent-fleet", stage="production", triggered_by="alice",
                               owner_approval="bob", production_identity="deployer")
        with self.assertRaisesRegex(ReleasePolicyError, "agent-fleet-production is not automated: owner hold"):
            build_release_plan(CATALOG, units, request, manifest(), "a" * 64)

    def test_rehearsal_groups_with_no_production_units_refuse_production(self) -> None:
        request = self.request(branch="release/rag-pipeline", components=("rag-pipeline",), stage="production",
                               triggered_by="alice", owner_approval="bob", production_identity="deployer")
        with self.assertRaisesRegex(ReleasePolicyError, "no production units"):
            self.plan(request)


class VerifyPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manifest_path = Path(directory.name) / "image-manifest.json"
        self.manifest_path.write_text(json.dumps(manifest()))
        request = ReleaseRequest(True, True, "owner", "release/coordinator", SHA, SHA, "gke-cpu", "in-cluster",
                                 ("agents",), str(self.manifest_path), "evidence")
        self.plan = build_release_plan(CATALOG, UNITS, request, manifest(), manifest_sha256(self.manifest_path))

    def verify(self, plan: dict, head: str | None = SHA) -> None:
        verify_release_plan(CATALOG, UNITS, plan, head_sha=head, manifest_path=self.manifest_path)

    def test_a_fresh_plan_verifies(self) -> None:
        self.verify(self.plan)

    def test_stale_and_tampered_plans_are_refused(self) -> None:
        reordered = copy.deepcopy(self.plan)
        reordered["units"].reverse()
        duplicated = copy.deepcopy(self.plan)
        duplicated["units"].append(copy.deepcopy(duplicated["units"][0]))
        retargeted = copy.deepcopy(self.plan)
        retargeted["units"][0]["target_namespace"] = "ewallet-mcp"
        swapped = copy.deepcopy(self.plan)
        swapped["units"][0]["images"]["mcp-tools"] = reference("other")
        unknown = copy.deepcopy(self.plan)
        unknown["units"][0]["name"] = "llm-serving"
        legacy = copy.deepcopy(self.plan)
        legacy["schema_version"] = 1
        handler = copy.deepcopy(self.plan)
        handler["units"][0]["handler"] = "scripts/llm_agent/deploy_atomic.sh"
        cases = ((reordered, "dependency order"), (duplicated, "dependency order"), (retargeted, "no longer matches"),
                 (swapped, "not the manifest digest"), (unknown, "unknown"), (legacy, "unsupported release plan schema"),
                 (handler, "removed handler"))
        for plan, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ReleasePolicyError, message):
                self.verify(plan)

    def test_a_plan_for_another_commit_is_refused(self) -> None:
        with self.assertRaisesRegex(ReleasePolicyError, "stale plan"):
            self.verify(self.plan, head="f" * 40)

    def test_a_changed_manifest_is_refused(self) -> None:
        self.manifest_path.write_text(json.dumps(manifest()) + "\n")
        with self.assertRaisesRegex(ReleasePolicyError, "changed after the plan"):
            self.verify(self.plan)


class ReleaseUnitCatalogTests(unittest.TestCase):
    def load(self, mutate) -> None:  # type: ignore[no-untyped-def]
        data = json.loads((ROOT / "jenkins/catalog/release-units.json").read_text())
        mutate(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "units.json"
            path.write_text(json.dumps(data))
            load_release_units(path).validate(ROOT, CATALOG)

    def test_unsafe_or_inconsistent_units_are_refused(self) -> None:
        def cycle(data):
            data["units"]["mcp-servers"]["depends_on"] = ["coordinator"]

        def production_namespace(data):
            data["units"]["mcp-servers"]["target_namespace"] = "ewallet-mcp"

        def handler(data):
            data["units"]["rag-pipeline"]["handler"] = "scripts/llm_agent/deploy_atomic.sh"

        def unknown_image(data):
            data["units"]["coordinator"]["images"] = ["g3-byo-feature-rag-specialist"]

        def open_closure(data):
            data["groups"]["release/coordinator"]["rehearsal"] = ["coordinator"]

        def unknown_kind(data):
            data["units"]["rag-pipeline"]["kind"] = "kubectl-apply"

        def production_in_rehearsal(data):
            data["units"]["coordinator-production"]["stage"] = "rehearsal"

        def wrong_release(data):
            data["units"]["coordinator-production"]["helm"]["namespace"] = "ewallet-agents"

        def no_verification(data):
            data["units"]["agent-fleet"]["verify"] = []

        def blocked_without_reason(data):
            data["units"]["agent-fleet-production"]["automation"] = "blocked"

        def fleet_without_candidate(data):
            data["units"]["agent-fleet-production"]["depends_on"] = []

        def candidate_elsewhere(data):
            data["units"]["agent-fleet-candidate"]["target_namespace"] = "ewallet-agents"

        def candidate_without_contract(data):
            del data["units"]["agent-fleet-candidate"]["contract"]

        def candidate_in_rehearsal(data):
            data["units"]["agent-fleet-candidate"]["stage"] = "rehearsal"

        cases = ((cycle, "cycle"), (production_namespace, "not the CD namespace"), (handler, "unknown fields"),
                 (unknown_image, "no component builds"), (open_closure, "does not deploy"),
                 (unknown_kind, "unsupported kind"), (production_in_rehearsal, "cannot run in stage"),
                 (wrong_release, "known Helm release"), (no_verification, "declares no runtime verification"),
                 (blocked_without_reason, "must say why"),
                 (fleet_without_candidate, "must depend on a sandbox candidate"),
                 (candidate_elsewhere, "SandboxAgent units target ewallet-sandbox"),
                 (candidate_without_contract, "needs contract"),
                 (candidate_in_rehearsal, "cannot run in stage"))
        for mutate, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ReleaseUnitError, message):
                self.load(mutate)

    def test_no_unit_deploys_a_legacy_specialist_or_the_production_llm_agent_values(self) -> None:
        text = (ROOT / "jenkins/catalog/release-units.json").read_text()
        for forbidden in ("deploy_atomic", "agent-cd-specialist", "feature-rag-specialist\": {\"byo"):
            self.assertNotIn(forbidden, text)
        for unit in UNITS.units.values():
            if unit.stage == "rehearsal" and unit.kind not in {"streaming-verification", "registry-gate"}:
                self.assertEqual("ewallet-cicd", unit.target_namespace, unit.name)


if __name__ == "__main__":
    unittest.main()


class ImageLessReleaseTests(unittest.TestCase):
    """The Prompt Management group deploys no image; its release must still plan (regression, Batch 2)."""

    def test_prompt_management_plans_with_an_empty_manifest(self) -> None:
        request = ReleaseRequest(True, True, "owner", "release/prompt-management", SHA, SHA, "gke-cpu", "in-cluster",
                                 ("prompt-management",), "m.json", "e")
        plan = build_release_plan(CATALOG, UNITS, request, {"git_sha": SHA, "images": {}}, "a" * 64)
        self.assertEqual([("prompt-registry-gate", {})], [(unit["name"], unit["images"]) for unit in plan["units"]])

    def test_an_empty_manifest_still_fails_a_unit_that_needs_an_image(self) -> None:
        request = ReleaseRequest(True, True, "owner", "release/rag-pipeline", SHA, SHA, "gke-cpu", "in-cluster",
                                 ("rag-pipeline",), "m.json", "e")
        with self.assertRaisesRegex(ReleasePolicyError, "no immutable digest"):
            build_release_plan(CATALOG, UNITS, request, {"git_sha": SHA, "images": {}}, "a" * 64)
