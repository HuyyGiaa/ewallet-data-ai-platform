"""Release safety (Batch 2A.1): verdict correctness, transaction recovery, registry lifecycle, handoff, locks.

Every external system is simulated: ``FakeKube`` (kagent/Substrate objects), ``FakeCluster`` (Helm),
fake Lease APIs and fake agent_replay helpers. Nothing here is runtime evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from scripts.ci import fleet_promote, production_release, registry_release, release_handoff, runtime_verify
from scripts.ci import sandbox_candidate
from scripts.ci.catalog import load_catalog
from scripts.ci.k8s_api import KubeError
from scripts.ci.release_lock import LockError, LockLost, ReleaseLocks
from scripts.ci.release_policy import ReleaseRequest, build_release_plan, manifest_sha256
from scripts.ci.release_units import load_release_units
from tests.ci.test_agent_cicd import (CONTRACT, MCP_NEW, NEW, OLD, SHA, FakeKube, fake_post, loopback, manifest,
                                      production_plan, specialist_reply)
from tests.ci.test_promote import FakeCluster

ROOT = Path(__file__).resolve().parents[2]
EXEC = "20261118t101500-abc123"


# --- runtime verdict: fail-closed reconciliation -------------------------------------------------------------
EXPECTED_IMAGES = {"ewallet-sandbox/SandboxAgent/drift-specialist": NEW, "ewallet-agents/Deployment/fraud-coordinator": NEW}


def snapshot(replay: Path, *, helm: dict[str, int], fail: tuple[str, ...] = (), images: dict | None = None,
             verdict: object = "auto", extra_rows: list | None = None, raw: str | None = None,
             frozen_revision: dict[str, int] | None = None) -> Path:
    """The files agent_replay.sh status writes, with replay_verdict's real row and detail formats."""

    status = replay / "status"
    status.mkdir(parents=True, exist_ok=True)
    rows: list = []
    for release, revision in helm.items():
        shown = (frozen_revision or {}).get(release, revision)
        if f"Helm {release}" in fail:
            rows.append(["FAIL", f"Helm {release}", f"revision {shown} deployed; expected [59, 60]"])
        else:
            rows.append(["PASS", f"Helm {release}", f"revision {shown} deployed: current"])
    rows.append(["PASS", "Coordinator Deployment fraud-coordinator (KEDA floor 2)", "ready 2/2"])
    rows += [["FAIL", check, "conditions {}"] for check in fail if not check.startswith("Helm ")]
    rows += extra_rows or []
    if verdict == "auto":
        verdict = "FAIL" if any(r[0] == "FAIL" for r in rows) else "PASS"
    if raw is not None:
        (status / "verdict.json").write_text(raw)
    elif verdict is not None:
        (status / "verdict.json").write_text(json.dumps({"verdict": verdict, "rows": rows}))
    for release, revision in helm.items():
        (status / f"helm-{release}.json").write_text(json.dumps([{"revision": revision, "status": "deployed"}]))
    agents, deployments = [], {"ewallet-agents": [], "ewallet-mcp": []}
    for key, image in (EXPECTED_IMAGES if images is None else images).items():
        namespace, kind, name = key.split("/")
        if kind == "SandboxAgent":
            agents.append({"metadata": {"name": name}, "spec": {"byo": {"deployment": {"image": image}}}})
        else:
            deployments[namespace].append({"metadata": {"name": name},
                                           "spec": {"template": {"spec": {"containers": [{"image": image}]}}}})
    for namespace, items in deployments.items():
        (status / f"deployments-{namespace}.json").write_text(json.dumps({"items": items}))
    (status / "sandboxagents.json").write_text(json.dumps({"items": agents}))
    return status


def reconcile(tmp_path: Path, exit_code: int, expected_revisions: dict[str, int], **kwargs) -> dict:  # type: ignore[no-untyped-def]
    snapshot(tmp_path, **kwargs)
    return runtime_verify.reconcile_status(tmp_path, {"helm_revisions": expected_revisions, "images": EXPECTED_IMAGES},
                                           exit_code)


def test_frozen_pass_with_matching_identity_is_pass(tmp_path) -> None:
    result = reconcile(tmp_path, 0, {"llm-agent": 60}, helm={"llm-agent": 60})
    assert result["status"] == "PASS" and result["accepted_exceptions"] == []


def test_one_historical_revision_mismatch_is_accepted_and_visible(tmp_path) -> None:
    result = reconcile(tmp_path, 1, {"llm-agent": 61, "llm-agent-mcp": 19}, helm={"llm-agent": 61, "llm-agent-mcp": 19},
                       fail=("Helm llm-agent",))
    assert result["status"] == "PASS_WITH_ACCEPTED_EXCEPTIONS"
    assert result["accepted_exceptions"] == [{"kind": "HISTORICAL_HELM_REVISION", "check": "Helm llm-agent",
                                              "frozen_detail": "revision 61 deployed; expected [59, 60]",
                                              "accepted_revision": 61}]


@pytest.mark.parametrize(("kwargs", "exit_code", "reason"), [
    (dict(fail=("SandboxAgent drift-specialist",)), 1, "RUNTIME_FAILURE"),                          # unrelated failure
    (dict(fail=("Helm llm-agent", "MCP server ewallet-mcp/drift-mcp")), 1, "RUNTIME_FAILURE"),       # several, one permitted
    (dict(fail=("Helm llm-agent",)), 2, "UNEXPECTED_COMMAND_FAILURE"),                               # crash with a verdict
    (dict(), 127, "UNEXPECTED_COMMAND_FAILURE"),                                                     # PASS file, bad exit
    (dict(verdict=None), 1, "MISSING_VERDICT"),
    (dict(raw="{not json"), 1, "MALFORMED_VERDICT"),
    (dict(raw=json.dumps({"verdict": "PASS", "rows": []})), 0, "MALFORMED_VERDICT"),
    (dict(raw=json.dumps({"verdict": "UNKNOWN", "rows": [["PASS", "x", "y"]]})), 0, "MALFORMED_VERDICT"),
    (dict(verdict="PASS", fail=("SandboxAgent drift-specialist",)), 0, "INCONSISTENT_VERDICT"),
    (dict(images={**EXPECTED_IMAGES, "ewallet-agents/Deployment/fraud-coordinator": OLD}), 0, "IDENTITY_MISMATCH"),
    (dict(images={}), 0, "IDENTITY_MISMATCH"),                                                      # resource missing
    (dict(fail=("Helm llm-agent",), frozen_revision={"llm-agent": 62}), 1, "HELM_REVISION_NOT_RECONCILED"),
])
def test_no_unexpected_failure_becomes_pass(tmp_path, kwargs, exit_code, reason) -> None:
    helm = kwargs.pop("helm", {"llm-agent": 61})
    result = reconcile(tmp_path, exit_code, {"llm-agent": 61}, helm=helm, **kwargs)
    assert result["status"] == "FAIL"
    assert any(item.startswith(reason) for item in result["reasons"]), result["reasons"]


def test_a_verdict_with_only_helm_rows_is_not_meaningful(tmp_path) -> None:
    status = tmp_path / "status"
    snapshot(tmp_path, helm={"llm-agent": 61})
    (status / "verdict.json").write_text(json.dumps({"verdict": "FAIL", "rows": [
        ["FAIL", "Helm llm-agent", "revision 61 deployed; expected [59, 60]"]]}))
    result = runtime_verify.reconcile_status(tmp_path, {"helm_revisions": {"llm-agent": 61}, "images": EXPECTED_IMAGES}, 1)
    assert result["status"] == "FAIL" and any(r.startswith("NO_RUNTIME_ROWS") for r in result["reasons"])


def test_an_empty_identity_expectation_fails(tmp_path) -> None:
    snapshot(tmp_path, helm={"llm-agent": 60})
    result = runtime_verify.reconcile_status(tmp_path, {"helm_revisions": {"llm-agent": 60}, "images": {}}, 0)
    assert result["status"] == "FAIL" and any(r.startswith("NO_IDENTITY_EXPECTATION") for r in result["reasons"])


def helper(behaviour: dict):  # type: ignore[no-untyped-def]
    def run(argv, env):  # type: ignore[no-untyped-def]
        args = list(argv[1:])
        replay = Path(env["REPLAY_DIR"])
        if args == ["status"]:
            return behaviour["status"](replay)
        path = replay / "e2e" / f"{args[-1]}-verdict.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"verdict": behaviour.get(args[-1], "COMPLETE")}))
        return 0 if behaviour.get(args[-1], "COMPLETE") == "COMPLETE" else 1
    return run


def test_run_checks_accepts_only_the_reconciled_exception_and_still_needs_every_e2e(tmp_path) -> None:
    def status(replay):  # type: ignore[no-untyped-def]
        snapshot(replay, helm={"llm-agent": 61}, fail=("Helm llm-agent",))
        return 1
    expected = {"helm_revisions": {"llm-agent": 61}, "images": EXPECTED_IMAGES}
    ok = runtime_verify.run_checks(["platform-status", "e2e-fraud"], tmp_path / "a", run=helper({"status": status}),
                                   environment={}, expected=expected)
    assert ok["verdict"] == "PASS" and ok["accepted_exceptions"][0]["check"] == "Helm llm-agent"
    assert ok["checks"][0]["status"] == "PASS_WITH_ACCEPTED_EXCEPTIONS"
    bad = runtime_verify.run_checks(["platform-status", "e2e-fraud"], tmp_path / "b",
                                    run=helper({"status": status, "fraud": "PARTIAL"}), environment={}, expected=expected)
    assert bad["verdict"] == "FAIL"  # a failed business E2E is never reconciled


def test_a_status_command_that_writes_no_verdict_fails(tmp_path) -> None:
    result = runtime_verify.run_checks(["platform-status"], tmp_path, run=helper({"status": lambda replay: 0}),
                                       environment={}, expected={"helm_revisions": {}, "images": EXPECTED_IMAGES})
    assert result["verdict"] == "FAIL" and "MISSING_VERDICT" in result["reconciliation"][0]["reasons"][0]


# --- production transaction -----------------------------------------------------------------------------------
def full_stack_plan() -> dict:
    units = json.loads((ROOT / "jenkins/catalog/release-units.json").read_text())
    planned = []
    for name in units["groups"]["release/coordinator"]["production"]:
        item = {**units["units"][name], "name": name}
        item["images"] = {image: (MCP_NEW if image == "mcp-tools" else NEW) for image in item["images"]}
        planned.append(item)
    return {**production_plan(*planned), "release_branch": "release/coordinator"}


def transaction(kube: FakeKube, helm: FakeCluster, verify=lambda expected: True, *, execute: bool = True,  # type: ignore[no-untyped-def]
                checkpoint=None, journal: Path | None = None, monkeypatch=None, post=None):
    original = sandbox_candidate.run

    def run(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.update(contract=CONTRACT, post=post or fake_post(), sleep=lambda s: None,
                      clock=iter(range(0, 100000, 7)).__next__)
        return original(*args, **kwargs)

    sandbox_candidate.run = run
    try:
        return production_release.run(full_stack_plan(), "gke-prod", manifest(), execute=execute, kube=kube,
                                      helm_runner=helm, verify=verify, forward=loopback, checkpoint=checkpoint,
                                      journal_path=journal, execution_id=EXEC)
    finally:
        sandbox_candidate.run = original


@pytest.fixture(autouse=True)
def fast_fleet(monkeypatch):  # type: ignore[no-untyped-def]
    original = fleet_promote.wait_healthy

    def wait(kube, contract, agent, image, *, timeout, clock, sleep):  # type: ignore[no-untyped-def]
        return original(kube, contract, agent, image, timeout=timeout, clock=iter(range(0, 100000, 7)).__next__,
                        sleep=lambda s: None)
    monkeypatch.setattr(fleet_promote, "wait_healthy", wait)
    monkeypatch.setenv("PROMOTION_APPROVED", "yes")


def test_a_verified_full_stack_release_is_deployed_pending_registry(tmp_path) -> None:
    kube, helm = FakeKube(), FakeCluster()
    seen: dict = {}
    report = transaction(kube, helm, verify=lambda expected: seen.update(expected) or True, journal=tmp_path / "j.json")
    assert report["verdict"] == "DEPLOYED_PENDING_REGISTRY", report["failure"]
    assert report["registry"]["status"] == "PENDING_AUTHORIZATION"
    assert report["registry"]["agents"] == ["drift-specialist", "feature-rag-specialist", "fraud-coordinator"]
    assert seen["helm_revisions"] == {"llm-agent": 60, "llm-agent-mcp": 20}
    assert seen["images"]["ewallet-sandbox/SandboxAgent/drift-specialist"] == NEW
    journal = json.loads((tmp_path / "j.json").read_text())
    assert journal["state"] == "DEPLOYED_PENDING_REGISTRY" and journal["execution_id"] == EXEC
    kinds = [e["event"] for e in journal["events"]]
    assert kinds.index("helm-upgrade-attempted") < kinds.index("helm-upgraded")
    assert "fleet-patch-attempted" in kinds and "candidates-deleted" in kinds
    assert not report["recovery"]["recovery_required"]  # only the owner's snapshot-prefix cleanup and registry step


def test_an_mcp_only_release_needs_no_registry_publication() -> None:
    plan = full_stack_plan()
    plan["units"] = [u for u in plan["units"] if u["name"] == "mcp-servers-production"]
    report = production_release.run(plan, "gke-prod", manifest(), execute=True, kube=FakeKube(), helm_runner=FakeCluster(),
                                    verify=lambda expected: True, forward=loopback, execution_id=EXEC)
    assert report["verdict"] == "RELEASE_COMPLETE" and report["registry"]["status"] == "NOT_REQUIRED"


def test_execute_without_a_verifier_is_refused_before_any_mutation() -> None:
    kube, helm = FakeKube(), FakeCluster()
    with pytest.raises(production_release.ProductionReleaseError, match="runtime verifier"):
        production_release.run(full_stack_plan(), "gke-prod", manifest(), execute=True, kube=kube, helm_runner=helm,
                               verify=None, forward=loopback)
    assert kube.calls == [] and helm.calls == []


@pytest.mark.parametrize("verify", [lambda expected: False, lambda expected: None, lambda expected: "PASS"])
def test_a_failed_or_non_boolean_verification_rolls_everything_back(verify) -> None:
    kube, helm = FakeKube(), FakeCluster()
    report = transaction(kube, helm, verify=verify)
    assert report["verdict"] == "FAILED_ROLLED_BACK" and report["verification"]["status"] == "FAIL"
    assert [u["status"] for u in report["units"]["helm"]] == ["ROLLED_BACK", "ROLLED_BACK"]
    assert [u["status"] for u in report["units"]["fleet"]] == ["ROLLED_BACK", "ROLLED_BACK"]
    assert [c[2] for c in helm.calls if c[:2] == ["helm", "rollback"]] == ["llm-agent", "llm-agent-mcp"]


def test_a_verifier_that_raises_enters_the_rollback_path() -> None:
    def explode(expected):  # type: ignore[no-untyped-def]
        raise RuntimeError("verifier crashed")
    kube, helm = FakeKube(), FakeCluster()
    report = transaction(kube, helm, verify=explode)
    assert report["verdict"] == "FAILED_ROLLED_BACK"
    assert report["failure"].startswith("runtime verification raised RuntimeError")  # the primary cause is kept
    assert report["verification"]["status"] == "ERROR"
    assert kube.get("SandboxAgent", "ewallet-sandbox", "drift-specialist")["spec"]["byo"]["deployment"]["image"] == OLD


def test_a_partial_helm_upgrade_is_detected_and_rolled_back() -> None:
    kube, helm = FakeKube(), FakeCluster()
    helm.partial_upgrade_of = "llm-agent-mcp"  # the cluster moved to revision 20, then the upgrade "timed out"
    report = transaction(kube, helm)
    assert report["verdict"] == "FAILED_ROLLED_BACK" and report["failed_unit"] == "mcp-servers-production"
    assert report["units"]["helm"][0]["status"] == "ROLLED_BACK"
    assert "failed upgrade left revision 20" in report["units"]["helm"][0]["detail"][0]
    assert [c[2:4] for c in helm.calls if c[:2] == ["helm", "rollback"]] == [["llm-agent-mcp", "19"]]


def test_a_failed_upgrade_that_changed_nothing_is_not_rolled_back() -> None:
    kube, helm = FakeKube(), FakeCluster(fail_upgrade_of="llm-agent-mcp")
    report = transaction(kube, helm)
    assert report["verdict"] == "FAILED_NOTHING_CHANGED"
    assert report["units"]["helm"][0]["status"] == "NOT_CHANGED"
    assert not [c for c in helm.calls if c[:2] == ["helm", "rollback"]]


def test_a_failed_post_upgrade_readback_rolls_back() -> None:
    kube = FakeKube()
    helm = FakeCluster(drift_after=f"{NEW.rsplit('@', 1)[0]}@sha256:" + "f" * 64)
    report = transaction(kube, helm)
    assert report["verdict"] == "FAILED_ROLLED_BACK"
    assert report["units"]["helm"][0]["status"] == "ROLLED_BACK"


@pytest.mark.parametrize("break_it", [lambda h: setattr(h, "fail_rollback_of", "llm-agent"),
                                      lambda h: setattr(h, "rollback_state", "failed")])
def test_a_helm_rollback_that_fails_or_does_not_deploy_is_rollback_failed(break_it) -> None:
    kube, helm = FakeKube(), FakeCluster()
    break_it(helm)
    report = transaction(kube, helm, verify=lambda expected: False)
    assert report["verdict"] == "ROLLBACK_FAILED" and report["rollback_errors"]
    assert report["recovery"]["recovery_required"]


def test_a_fleet_patch_failure_on_the_second_agent_rolls_back_mcp_and_the_first_agent() -> None:
    kube, helm = FakeKube(patch_fails_for="drift-specialist"), FakeCluster()
    report = transaction(kube, helm)
    assert report["verdict"] == "FAILED_ROLLED_BACK" and report["failed_unit"] == "agent-fleet-production"
    assert [u["status"] for u in report["units"]["fleet"]] == ["ROLLED_BACK", "NOT_CHANGED"]
    assert report["units"]["helm"][0]["status"] == "ROLLED_BACK"


def test_a_sandbox_agent_ready_timeout_rolls_back() -> None:
    kube, helm = FakeKube(), FakeCluster()
    seen = {"n": 0}

    def checkpoint() -> None:  # after the candidate passed, the production fleet stops becoming Ready
        seen["n"] += 1
        if seen["n"] == 2:
            kube.behavior = "patch-not-ready"

    report = transaction(kube, helm, checkpoint=checkpoint)
    assert report["verdict"] == "FAILED_ROLLED_BACK" and "did not become Ready" in report["failure"]


def test_a_concurrent_sandbox_agent_change_is_never_overwritten() -> None:
    kube, helm = FakeKube(), FakeCluster()

    def verify(expected):  # type: ignore[no-untyped-def]
        kube.objects[("SandboxAgent", "ewallet-sandbox", "drift-specialist")]["spec"]["byo"]["deployment"]["image"] = "x"
        return False

    report = transaction(kube, helm, verify=verify)
    assert report["verdict"] == "ROLLBACK_FAILED"
    assert kube.get("SandboxAgent", "ewallet-sandbox", "drift-specialist")["spec"]["byo"]["deployment"]["image"] == "x"


def test_a_failed_candidate_changes_nothing() -> None:
    kube, helm = FakeKube(create_fails_for="feature-rag-specialist-cicd"), FakeCluster()
    report = transaction(kube, helm)
    assert report["verdict"] == "FAILED_NOTHING_CHANGED" and not report["production_changed"]
    assert not [c for c in helm.calls if c[:2] in (["helm", "upgrade"], ["helm", "rollback"])]


def test_an_incomplete_candidate_cleanup_requires_recovery() -> None:
    kube, helm = FakeKube(delete_fails=True), FakeCluster()
    report = transaction(kube, helm)
    assert report["verdict"] == "RECOVERY_REQUIRED" and report["candidate_cleanup_incomplete"]
    actions = {a["action"] for a in report["recovery"]["actions"]}
    assert "delete-candidate" in actions and report["recovery"]["recovery_required"]


def test_a_lost_lock_stops_mutations_without_an_automatic_rollback() -> None:
    kube, helm = FakeKube(), FakeCluster()
    calls = {"n": 0}

    def checkpoint() -> None:
        calls["n"] += 1
        if calls["n"] == 4:  # before the fleet unit: MCP is already upgraded
            raise LockLost("LOCK_LOST: release lock production-release is now held by another-build")

    report = transaction(kube, helm, checkpoint=checkpoint)
    assert report["verdict"] == "RECOVERY_REQUIRED" and report["lock_lost"]
    assert not [c for c in helm.calls if c[:2] == ["helm", "rollback"]]  # never races the new holder
    assert any(a["action"] == "helm-inspect-then-rollback" for a in report["recovery"]["actions"])
    assert all(u["status"] != "PATCHED" for u in report["units"]["fleet"])


def test_an_interrupted_operator_process_leaves_a_deterministic_recovery_plan(tmp_path) -> None:
    kube, helm = FakeKube(), FakeCluster()
    original = helm.__call__

    class Interrupted(BaseException):
        pass

    def interrupting(command):  # type: ignore[no-untyped-def]
        if list(command[:3]) == ["helm", "upgrade", "llm-agent"]:
            raise Interrupted()  # e.g. the operator's laptop sleeps or the process is killed
        return original(command)

    journal = tmp_path / "transaction-journal.json"
    with pytest.raises(Interrupted):
        transaction(kube, FakeClusterProxy(helm, interrupting), journal=journal)
    data = json.loads(journal.read_text())
    assert data["state"] == "STARTED"  # never finished
    plan = production_release.recovery_plan(data)
    actions = [a["action"] for a in plan["actions"]]
    assert plan["recovery_required"]
    assert actions.count("helm-inspect-then-rollback") == 2  # MCP upgraded, Coordinator attempted
    assert actions.count("sandboxagent-restore-image") == 2
    assert "gcs-remove-exact-prefix" in actions and "verify-runtime" in actions
    assert production_release.recovery_plan(data) == plan  # deterministic
    assert production_release.main(["recovery-plan", "--journal", str(journal)]) == 1


class FakeClusterProxy:
    def __init__(self, cluster: FakeCluster, call) -> None:  # type: ignore[no-untyped-def]
        self.cluster, self.call = cluster, call

    def __call__(self, command):  # type: ignore[no-untyped-def]
        return self.call(command)

    @property
    def calls(self):  # type: ignore[no-untyped-def]
        return self.cluster.calls


def test_without_approval_nothing_changes_and_nothing_is_verified(monkeypatch) -> None:
    monkeypatch.setenv("PROMOTION_APPROVED", "no")
    kube, helm = FakeKube(), FakeCluster()
    verified: list = []
    report = transaction(kube, helm, verify=lambda expected: verified.append(1) or True)
    assert report["verdict"] == "CHECKED_NOT_EXECUTED" and kube.calls == [] and verified == []


def test_registry_publication_never_happens_inside_the_transaction() -> None:
    kube, helm = FakeKube(), FakeCluster()
    report = transaction(kube, helm)
    commands = [step["command"] for step in report["registry"]["procedure"]]
    publish = [c for c in commands if "--confirm-live-write" in c]
    assert len(publish) == 1 and report["registry"]["procedure"][2]["authorization"].startswith("separate owner approval")
    assert not [c for c in helm.calls if "registry_catalog" in " ".join(c)]


# --- registry lifecycle ------------------------------------------------------------------------------------------
def pending() -> dict:
    return {"verdict": "DEPLOYED_PENDING_REGISTRY", "git_sha": SHA,
            "registry": {"status": "PENDING_AUTHORIZATION", "agents": ["drift-specialist", "fraud-coordinator"]}}


def readback(sha: str = SHA, published: str = SHA, **changes) -> dict:  # type: ignore[no-untyped-def]
    entries = [{"kind": "Agent", "name": name, "registered": True, "matches": True, "published_git_sha": published}
               for name in ("drift-specialist", "feature-rag-specialist", "fraud-coordinator")]
    entries.append({"kind": "Prompt", "name": "x", "registered": True, "matches": True, "published_git_sha": None})
    report = {"verdict": "PASS", "git_sha": sha, "entries": entries}
    report.update(changes)
    return report


def test_registry_reconciliation_completes_the_release_only_with_capture_and_strict_readback() -> None:
    result = registry_release.reconcile(pending(), readback(published="0" * 40), readback())
    assert result["verdict"] == "RELEASE_COMPLETE"
    assert result["previous_published_sha"] == {"drift-specialist": "0" * 40, "fraud-coordinator": "0" * 40}


@pytest.mark.parametrize(("before", "after", "reason"), [
    (None, readback(), "NO_PRE_PUBLICATION_CAPTURE"),
    (readback(entries=[]), readback(), "CAPTURE_INCOMPLETE"),
    (readback(), None, "NO_READBACK"),                                              # publication unavailable
    (readback(), readback(verdict="FAIL"), "READBACK_FAILED"),
    (readback(), readback(sha="d" * 40), "READBACK_WRONG_RELEASE"),
    (readback(), readback(published="0" * 40), "NOT_PUBLISHED_AT_RELEASE"),
    (readback(), {**readback(), "entries": [{**e, "matches": False} for e in readback()["entries"]]}, "READBACK_MISMATCH"),
])
def test_anything_less_is_registry_reconciliation_failed(before, after, reason) -> None:
    result = registry_release.reconcile(pending(), before, after)
    assert result["verdict"] == "REGISTRY_RECONCILIATION_FAILED"
    assert any(r.startswith(reason) for r in result["reasons"]), result["reasons"]


@pytest.mark.parametrize("state", ["FAILED_ROLLED_BACK", "ROLLBACK_FAILED", "RECOVERY_REQUIRED", "CHECKED_NOT_EXECUTED"])
def test_only_a_verified_deployment_can_be_reconciled(state) -> None:
    result = registry_release.reconcile({**pending(), "verdict": state}, readback(), readback())
    assert result["verdict"] == "REGISTRY_RECONCILIATION_FAILED"


def test_registry_cli_writes_a_fail_closed_verdict(tmp_path) -> None:
    (tmp_path / "release.json").write_text(json.dumps(pending()))
    (tmp_path / "before.json").write_text(json.dumps(readback()))
    (tmp_path / "after.json").write_text("garbage")
    code = registry_release.main(["reconcile", "--release-verdict", str(tmp_path / "release.json"), "--before",
                                  str(tmp_path / "before.json"), "--after", str(tmp_path / "after.json"),
                                  "--output", str(tmp_path / "out.json")])
    assert code == 1 and json.loads((tmp_path / "out.json").read_text())["verdict"] == "REGISTRY_RECONCILIATION_FAILED"


# --- Jenkins -> operator handoff ---------------------------------------------------------------------------------
def handoff_sources(tmp_path: Path, *, sha: str = SHA, source_sha: str | None = None) -> dict[str, Path]:
    catalog = load_catalog(ROOT / "jenkins/catalog/components.json")
    units = load_release_units(ROOT / "jenkins/catalog/release-units.json")
    reg = "asia-southeast1-docker.pkg.dev/p/ewallet-platform"
    images = {"git_sha": sha, "images": {n: {"digest_ref": f"{reg}/{n}@sha256:" + "d" * 64, "revision": sha}
                                         for n in ("mcp-tools", "g3-byo-coordinator")}}
    src = tmp_path / "jenkins"
    src.mkdir()
    (src / "image-manifest.json").write_text(json.dumps(images))
    request = ReleaseRequest(True, True, "owner-b", "release/coordinator", sha, sha, "gke-cpu", "gke-prod",
                             ("agents", "mcp-agents"), str(src / "image-manifest.json"), "evidence/production",
                             "production", "dev-a", "ewallet-cicd-agent-release")
    plan = build_release_plan(catalog, units, request, images, manifest_sha256(src / "image-manifest.json"))
    (src / "release-plan.json").write_text(json.dumps(plan))
    (src / "release-request.json").write_text(json.dumps(request.__dict__ | {"components": list(request.components)}))
    (src / "ci-verdict.json").write_text(json.dumps({"status": "PASS"}))
    (src / "image-scan.json").write_text(json.dumps({"verdict": "PASS"}))
    (src / "jenkins-build.txt").write_text(f"job_name=edai2-cd-coordinator\nbuild_number=42\nbuild_url=https://jenkins/job/42/\n"
                                           f"build_tag=jenkins-edai2-cd-coordinator-42\ngit_sha={sha}\ntriggered_by=dev-a\n")
    (src / "source-metadata.txt").write_text(f"source_sha={source_sha or sha}\nbundle_sha256={'e' * 64}\n")
    return {name: src / name for name in release_handoff.ARTIFACTS + release_handoff.OPTIONAL_ARTIFACTS}


def made_handoff(tmp_path: Path, **kwargs) -> tuple[Path, str]:  # type: ignore[no-untyped-def]
    out = tmp_path / "handoff"
    _, digest = release_handoff.build(out, handoff_sources(tmp_path, **kwargs))
    return out, digest


def test_a_handoff_records_the_release_and_verifies_end_to_end(tmp_path) -> None:
    out, digest = made_handoff(tmp_path)
    handoff, problems = release_handoff.verify(out, digest, head_sha=SHA, production_context="gke-prod",
                                               production_identity="ewallet-cicd-agent-release")
    assert problems == []
    assert handoff["status"] == "READY_FOR_OPERATOR_PROMOTION"
    assert handoff["jenkins"]["build_number"] == "42" and handoff["jenkins"]["build_url"].startswith("https://")
    assert handoff["approval"] == {"triggered_by": "dev-a", "production_approved_by": "owner-b"}
    assert [u["name"] for u in handoff["units"]] == ["agent-fleet-candidate", "mcp-servers-production",
                                                     "agent-fleet-production", "coordinator-production"]
    assert handoff["units"][2]["depends_on"] == ["agent-fleet-candidate"]
    assert "e2e-combined" in handoff["verification"]["runtime_checks"]
    assert handoff["source_snapshot"]["source_sha"] == SHA


@pytest.mark.parametrize(("mutate", "problem"), [
    (lambda out, d: (out / "release-plan.json").write_text((out / "release-plan.json").read_text().replace("gke-cpu", "dev")),
     "TAMPERED_ARTIFACT"),
    (lambda out, d: (out / "image-scan.json").unlink(), "MISSING_ARTIFACT"),
    (lambda out, d: (out / "handoff.json").write_text((out / "handoff.json").read_text().replace("owner-b", "dev-a")),
     "HANDOFF_DIGEST_MISMATCH"),
    (lambda out, d: (out / "handoff.json").unlink(), "MISSING_HANDOFF"),
])
def test_a_missing_or_tampered_handoff_is_refused(tmp_path, mutate, problem) -> None:
    out, digest = made_handoff(tmp_path)
    mutate(out, digest)
    _, problems = release_handoff.verify(out, digest, head_sha=SHA, production_context="gke-prod")
    assert any(p.startswith(problem) for p in problems), problems


def test_wrong_sha_target_identity_or_snapshot_are_refused(tmp_path) -> None:
    out, digest = made_handoff(tmp_path, source_sha="b" * 40)
    _, problems = release_handoff.verify(out, digest, head_sha="d" * 40, production_context="kind-local",
                                         production_identity="someone-else")
    joined = " ".join(problems)
    assert "SHA_MISMATCH: source snapshot" in joined and "SHA_MISMATCH: operator HEAD" in joined
    assert "WRONG_TARGET" in joined and "WRONG_IDENTITY" in joined and "PLAN_REFUSED" in joined


def test_a_wrong_immutable_digest_in_the_manifest_is_refused(tmp_path) -> None:
    sources = handoff_sources(tmp_path)
    manifest_path = sources["image-manifest.json"]
    data = json.loads(manifest_path.read_text())
    data["images"]["g3-byo-coordinator"]["digest_ref"] = data["images"]["g3-byo-coordinator"]["digest_ref"][:-1] + "0"
    manifest_path.write_text(json.dumps(data))  # after the plan was built: plan and manifest disagree
    out = tmp_path / "handoff"
    _, digest = release_handoff.build(out, sources)
    _, problems = release_handoff.verify(out, digest, head_sha=SHA, production_context="gke-prod")
    assert any(p.startswith("PLAN_REFUSED") for p in problems), problems


def test_a_rehearsal_plan_cannot_be_handed_off(tmp_path) -> None:
    sources = handoff_sources(tmp_path)
    plan = json.loads(sources["release-plan.json"].read_text())
    sources["release-plan.json"].write_text(json.dumps({**plan, "stage": "rehearsal"}))
    with pytest.raises(release_handoff.HandoffError):
        release_handoff.build(tmp_path / "handoff", sources)


def dispatch(tmp_path: Path, out: Path, digest: str, env: dict, context: str = "gke-prod") -> tuple[int, list]:  # type: ignore[type-arg]
    from scripts.ci import release_dispatch

    calls: list = []
    original = (release_dispatch._locks, release_dispatch._git_head)
    release_dispatch._locks = lambda *a: calls.append("locks")
    release_dispatch._git_head = lambda: SHA
    import os
    saved = dict(os.environ)
    os.environ.pop("AGI_ENV", None)
    os.environ.update(env)
    try:
        code = release_dispatch.main(["--plan", str(out / "release-plan.json"), "--execute", "--production-context", context,
                                      "--handoff", str(out), "--handoff-sha256", digest])
    finally:
        os.environ.clear()
        os.environ.update(saved)
        release_dispatch._locks, release_dispatch._git_head = original
    return code, calls


def test_the_operator_dispatch_refuses_before_any_lock(tmp_path) -> None:
    out, digest = made_handoff(tmp_path)
    good_env = tmp_path / "agi.env"
    good_env.write_text("CTX=gke-prod\n")
    other_env = tmp_path / "other.env"
    other_env.write_text("CTX=kind-local\n")
    for env, d, context in (({}, digest, "gke-prod"),                               # missing AGI_ENV
                            ({"AGI_ENV": str(other_env)}, digest, "gke-prod"),      # verification would read another cluster
                            ({"AGI_ENV": str(good_env)}, "0" * 64, "gke-prod"),     # not the announced handoff
                            ({"AGI_ENV": str(good_env)}, digest, "kind-local")):    # wrong production context
        code, calls = dispatch(tmp_path, out, d, env, context)
        assert (code, calls) == (2, [])


# --- release locks: renewal, expiry, ownership --------------------------------------------------------------
class Leases:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.version = 0
        self.fail_replace = False

    def _bump(self, document: dict) -> dict:
        self.version += 1
        document = json.loads(json.dumps(document))
        document["metadata"]["resourceVersion"] = str(self.version)
        self.items[document["metadata"]["name"]] = document
        return document

    def create(self, document: dict) -> dict:
        if document["metadata"]["name"] in self.items:
            raise KubeError("POST -> HTTP 409: AlreadyExists")
        return self._bump(document)

    def get(self, name: str) -> dict | None:
        item = self.items.get(name)
        return json.loads(json.dumps(item)) if item else None

    def delete_exact(self, name: str, resource_version: str) -> None:
        if self.items.get(name, {}).get("metadata", {}).get("resourceVersion") != resource_version:
            raise KubeError("DELETE -> HTTP 409: precondition failed")
        del self.items[name]

    def replace(self, document: dict) -> dict:
        current = self.items.get(document["metadata"]["name"])
        if self.fail_replace or current is None or current["metadata"]["resourceVersion"] != document["metadata"]["resourceVersion"]:
            raise KubeError("PUT -> HTTP 409: conflict")
        return self._bump(document)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 18, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


def test_renewal_extends_a_held_lock_and_release_uses_the_latest_version() -> None:
    api, clock = Leases(), Clock()
    locks = ReleaseLocks(api, "ewallet-cicd", "build-1", duration_seconds=600, clock=clock)
    with locks.hold(["production-release"]):
        first = api.get("release-lock-production-release")["metadata"]["resourceVersion"]
        clock.now += timedelta(seconds=500)
        locks.renew()
        assert api.get("release-lock-production-release")["metadata"]["resourceVersion"] != first
        clock.now += timedelta(seconds=500)  # past the original expiry, within the renewed one
        locks.renew()
    assert api.items == {}


def test_a_second_release_cannot_start_while_the_lock_is_held() -> None:
    api, clock = Leases(), Clock()
    one = ReleaseLocks(api, "ewallet-cicd", "build-1", clock=clock)
    two = ReleaseLocks(api, "ewallet-cicd", "build-2", clock=clock)
    with one.hold(["production-release"]):
        with pytest.raises(LockError, match="held by build-1"):
            with two.hold(["production-release"]):
                pass


def test_an_expired_lock_taken_over_makes_the_original_holder_stop() -> None:
    api, clock = Leases(), Clock()
    one = ReleaseLocks(api, "ewallet-cicd", "build-1", duration_seconds=600, clock=clock)
    two = ReleaseLocks(api, "ewallet-cicd", "build-2", duration_seconds=600, clock=clock)
    with pytest.raises(LockLost, match="now held by build-2"):
        with one.hold(["production-release"]):
            clock.now += timedelta(seconds=601)  # build-1 is slow; its lease expires
            with two.hold(["production-release"]):  # build-2 takes it over
                one.renew()  # build-1's next checkpoint
    # build-1's exit did not delete build-2's lock while build-2 held it (two released its own).
    assert api.items == {}


@pytest.mark.parametrize("break_it", ["expired", "replaced", "changed"])
def test_renewal_fails_closed(break_it) -> None:
    api, clock = Leases(), Clock()
    locks = ReleaseLocks(api, "ewallet-cicd", "build-1", duration_seconds=600, clock=clock)
    with pytest.raises(LockLost):
        with locks.hold(["production-release"]):
            if break_it == "expired":
                clock.now += timedelta(seconds=601)
            elif break_it == "replaced":
                api.fail_replace = True
            else:
                api._bump(api.get("release-lock-production-release"))  # someone else wrote it
            locks.renew()


def test_a_stale_holder_never_deletes_a_lock_acquired_by_another_holder() -> None:
    api, clock = Leases(), Clock()
    one = ReleaseLocks(api, "ewallet-cicd", "build-1", duration_seconds=600, clock=clock)
    two = ReleaseLocks(api, "ewallet-cicd", "build-2", duration_seconds=600, clock=clock)
    version = one.acquire_one("production-release")
    one.held["production-release"] = version
    clock.now += timedelta(seconds=601)
    two.acquire_one("production-release")
    with pytest.raises(LockError, match="no longer held by build-1"):
        one.release_one("production-release", version)
    assert api.get("release-lock-production-release")["spec"]["holderIdentity"] == "build-2"


@pytest.mark.parametrize(("state", "code"), [("RELEASE_COMPLETE", 0), ("DEPLOYED_PENDING_REGISTRY", 3),
                                             ("FAILED_ROLLED_BACK", 1), ("ROLLBACK_FAILED", 4), ("RECOVERY_REQUIRED", 4)])
def test_the_operator_dispatch_wires_locks_journal_and_lifecycle_exit_codes(tmp_path, monkeypatch, state, code) -> None:
    import contextlib
    import os

    from scripts.ci import release_dispatch

    out, digest = made_handoff(tmp_path)
    env_file = tmp_path / "agi.env"
    env_file.write_text("CTX=gke-prod\n")
    seen: dict = {}

    class Locks:
        def renew(self) -> None:
            seen["renewed"] = True

        @contextlib.contextmanager
        def hold(self, names):  # type: ignore[no-untyped-def]
            seen["locks"] = list(names)
            yield names

    @contextlib.contextmanager
    def proxy(context):  # type: ignore[no-untyped-def]
        seen["proxy_context"] = context
        yield "http://127.0.0.1:1"

    def run(plan, context, manifest_data, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs, plan_sha=plan["git_sha"], context=context)
        return {"verdict": state, "execution_id": kwargs["execution_id"], "production_changed": False, "failure": None}

    monkeypatch.setattr(production_release, "kubectl_proxy", proxy)
    monkeypatch.setattr(production_release, "run", run)
    monkeypatch.setattr(release_dispatch, "_locks", lambda namespace, holder, api: Locks())
    monkeypatch.setattr(release_dispatch, "_git_head", lambda: SHA)
    monkeypatch.setenv("AGI_ENV", str(env_file))
    result = release_dispatch.main(["--plan", str(out / "release-plan.json"), "--execute", "--production-context",
                                    "gke-prod", "--handoff", str(out), "--handoff-sha256", digest,
                                    "--evidence-dir", str(tmp_path / "operator")])
    assert result == code
    assert seen["locks"] == ["production-release"] and seen["proxy_context"] == "gke-prod"
    seen["checkpoint"]()
    assert seen["renewed"]  # the transaction's checkpoint is the lock renewal
    assert seen["journal_path"].name == "transaction-journal.json"
    assert seen["journal_path"].parent.parent == tmp_path / "operator"
    assert seen["handoff"]["sha256"] == digest and seen["handoff"]["jenkins"]["build_number"] == "42"
    assert seen["verify"] is not None and seen["plan_sha"] == SHA
    verdict = json.loads((seen["journal_path"].parent / "release-verdict.json").read_text())
    assert verdict["verdict"] == state
    assert os.environ.get("PROMOTION_APPROVED") == "yes"  # set by the fixture; the dispatcher never sets it
