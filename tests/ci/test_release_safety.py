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


def test_a_partial_helm_upgrade_that_ended_failed_is_rolled_back_and_restored() -> None:
    kube, helm = FakeKube(), FakeCluster()
    helm.partial_upgrade_of, helm.partial_state = "llm-agent-mcp", "failed"  # moved to 20, then "timed out"
    report = transaction(kube, helm)
    assert report["verdict"] == "FAILED_ROLLED_BACK" and report["failed_unit"] == "mcp-servers-production"
    assert report["units"]["helm"][0]["status"] == "ROLLED_BACK"
    assert "rollback from revision 20 (failed) to 19" in report["units"]["helm"][0]["detail"][0]
    assert [c[2:4] for c in helm.calls if c[:2] == ["helm", "rollback"]] == [["llm-agent-mcp", "19"]]


@pytest.mark.parametrize("state", ["pending-upgrade", "pending-rollback", "superseded"])
def test_a_moved_release_in_a_pending_or_odd_state_is_never_rolled_back_automatically(state) -> None:
    kube, helm = FakeKube(), FakeCluster()
    helm.partial_upgrade_of, helm.partial_state = "llm-agent-mcp", state
    report = transaction(kube, helm)
    assert report["verdict"] == "RECOVERY_REQUIRED" and report["state_unknown"] == ["mcp-servers-production"]
    assert not [c for c in helm.calls if c[:2] == ["helm", "rollback"]]  # no destructive retry
    assert any(a["action"] == "helm-observe-then-decide" for a in report["recovery"]["actions"])


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

    def checkpoint() -> None:  # once the candidate is cleaned up, the production fleet stops becoming Ready
        if any(c[0] == "delete" for c in kube.calls):
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
    assert "candidate-recovery" in actions and report["recovery"]["recovery_required"]


def test_a_lost_lock_stops_mutations_without_an_automatic_rollback() -> None:
    kube, helm = FakeKube(), FakeCluster()

    def checkpoint() -> None:
        if "llm-agent-mcp" in helm.upgraded:  # right after the MCP upgrade (the post-upgrade check)
            raise LockLost("LOCK_LOST: release lock production-release is now held by another-build")

    report = transaction(kube, helm, checkpoint=checkpoint)
    assert report["verdict"] == "RECOVERY_REQUIRED" and report["lock_lost"] and report["lock_state"] == "LOCK_LOST"
    assert report["recovery"]["actions"][0]["action"] == "lock-and-identity-check"
    assert not [c for c in helm.calls if c[:2] == ["helm", "rollback"]]  # never races the new holder
    assert any(a["action"] == "helm-observe-then-decide" for a in report["recovery"]["actions"])
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
    assert actions.count("helm-observe-then-decide") == 2  # MCP upgraded, Coordinator attempted
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
    src.mkdir(parents=True)
    (src / "image-manifest.json").write_text(json.dumps(images))
    request = ReleaseRequest(True, True, "owner-b", "release/coordinator", sha, sha, "gke-cpu", "gke-prod",
                             ("agents", "mcp-agents"), str(src / "image-manifest.json"), "evidence/production",
                             "production", "dev-a", IDENTITY)
    plan = build_release_plan(catalog, units, request, images, manifest_sha256(src / "image-manifest.json"))
    (src / "release-plan.json").write_text(json.dumps(plan))
    (src / "release-request.json").write_text(json.dumps(request.__dict__ | {"components": list(request.components)}))
    (src / "ci-verdict.json").write_text(json.dumps(ci_verdict(sha)))
    (src / "image-scan.json").write_text(json.dumps(scan_verdict(sha, images)))
    (src / "jenkins-build.txt").write_text(f"job_name=edai2-cd-coordinator\nbuild_number=42\nbuild_url=https://jenkins/job/42/\n"
                                           f"build_tag=jenkins-edai2-cd-coordinator-42\ngit_sha={sha}\ntriggered_by=dev-a\n")
    (src / "source-metadata.txt").write_text(
        f"source_sha={source_sha or sha}\nbase_sha={'0' * 40}\nref:refs/heads/release/coordinator={source_sha or sha}\n"
        f"bundle_sha256={'e' * 64}\n")
    return {name: src / name for name in release_handoff.ARTIFACTS + release_handoff.OPTIONAL_ARTIFACTS}


IDENTITY = "system:serviceaccount:ewallet-ci:ewallet-cicd-agent-release"


def _rewrite_artifact_digest(out: Path, name: str) -> None:
    """An attacker who can write the handoff also rewrites its checksums; only the console digest and the
    semantic checks remain. The test then hands the forged digest to verify, to exercise the semantics."""

    handoff = json.loads((out / "handoff.json").read_text())
    handoff["artifacts"][name]["sha256"] = release_handoff.sha256(out / name)
    (out / "handoff.json").write_text(json.dumps(handoff, indent=2, sort_keys=True) + "\n")


def forged(out: Path, name: str, mutate) -> str:  # type: ignore[no-untyped-def]
    path = out / name
    if name == "handoff.json":
        data = json.loads(path.read_text())
        mutate(data)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    else:
        data = json.loads(path.read_text())
        mutate(data)
        path.write_text(json.dumps(data))
        _rewrite_artifact_digest(out, name)
    return release_handoff.sha256(out / "handoff.json")


def ci_verdict(sha: str, **changes) -> dict:  # type: ignore[no-untyped-def]
    """The real scripts/ci/run_ci.py verdict shape."""

    verdict = {"status": "PASS", "reason": None, "profile": None, "git_sha": sha,
               "scope_components": ["agents", "mcp-agents"], "selected_components": ["agents", "mcp-agents"],
               "commands_executed": 4, "test_commands": 2, "check_commands": 2,
               "test_cases": {"tests": 915, "failures": 0, "errors": 0, "skipped": 3},
               "executed": [{"stage": f"s{i}", "kind": "test" if i % 2 else "check", "exit_code": 0,
                             "classification": None, "test_cases": None} for i in range(4)]}
    verdict.update(changes)
    return verdict


def scan_verdict(sha: str, manifest_data: dict, **changes) -> dict:  # type: ignore[no-untyped-def]
    """The real scripts/ci/image_scan.py gate shape."""

    from scripts.ci.image_scan import TRIVY

    verdict = {"verdict": "PASS", "scanner": TRIVY, "severities": ["HIGH", "CRITICAL"], "git_sha": sha,
               "images": {name: {"status": "PASS", "findings": [], "excepted": [], "expired_exceptions": [],
                                 "digest_ref": image["digest_ref"]} for name, image in manifest_data["images"].items()}}
    verdict.update(changes)
    return verdict


def made_handoff(tmp_path: Path, **kwargs) -> tuple[Path, str]:  # type: ignore[no-untyped-def]
    out = tmp_path / "handoff"
    _, digest = release_handoff.build(out, handoff_sources(tmp_path, **kwargs))
    return out, digest


def test_a_handoff_records_the_release_and_verifies_end_to_end(tmp_path) -> None:
    out, digest = made_handoff(tmp_path)
    handoff, problems = release_handoff.verify(out, digest, head_sha=SHA, production_context="gke-prod",
                                               production_identity=IDENTITY)
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
    _, problems = release_handoff.verify(out, digest, head_sha=SHA, production_context="gke-prod",
                                         production_identity=IDENTITY)
    assert any(p.startswith(problem) for p in problems), problems


def test_wrong_sha_target_identity_or_snapshot_are_refused(tmp_path) -> None:
    sources = handoff_sources(tmp_path)
    sources["source-metadata.txt"].write_text(sources["source-metadata.txt"].read_text().replace(SHA, "b" * 40))
    with pytest.raises(release_handoff.HandoffError, match="SOURCE_MISMATCH"):
        release_handoff.build(tmp_path / "refused", sources)  # Jenkins never announces such a handoff
    out, digest = made_handoff(tmp_path / "ok")
    metadata = out / "source-metadata.txt"
    metadata.write_text(metadata.read_text().replace(SHA, "b" * 40))
    _rewrite_artifact_digest(out, "source-metadata.txt")
    digest = release_handoff.sha256(out / "handoff.json")
    _, problems = release_handoff.verify(out, digest, head_sha="d" * 40, production_context="kind-local",
                                         production_identity="system:serviceaccount:ewallet-ci:someone-else")
    joined = " ".join(problems)
    assert "SOURCE_MISMATCH" in joined and "SHA_MISMATCH: operator HEAD" in joined
    assert "WRONG_TARGET" in joined and "WRONG_IDENTITY" in joined and "PLAN_REFUSED" in joined


def test_a_wrong_immutable_digest_in_the_manifest_is_refused(tmp_path) -> None:
    sources = handoff_sources(tmp_path)
    manifest_path = sources["image-manifest.json"]
    data = json.loads(manifest_path.read_text())
    data["images"]["g3-byo-coordinator"]["digest_ref"] = data["images"]["g3-byo-coordinator"]["digest_ref"][:-1] + "0"
    manifest_path.write_text(json.dumps(data))  # after the plan was built: plan and manifest disagree
    with pytest.raises(release_handoff.HandoffError, match="SCAN_DIGEST_MISMATCH"):
        release_handoff.build(tmp_path / "handoff", sources)  # the scan no longer matches the manifest either


def test_a_rehearsal_plan_cannot_be_handed_off(tmp_path) -> None:
    sources = handoff_sources(tmp_path)
    plan = json.loads(sources["release-plan.json"].read_text())
    sources["release-plan.json"].write_text(json.dumps({**plan, "stage": "rehearsal"}))
    with pytest.raises(release_handoff.HandoffError):
        release_handoff.build(tmp_path / "handoff", sources)


def dispatch(tmp_path: Path, out: Path, digest: str, env: dict, context: str = "gke-prod",  # type: ignore[type-arg]
             identity: str | None = IDENTITY, capsys=None) -> tuple[int, list]:
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
                                      "--handoff", str(out), "--handoff-sha256", digest,
                                      *(["--production-identity", identity] if identity else [])])
    finally:
        os.environ.clear()
        os.environ.update(saved)
        release_dispatch._locks, release_dispatch._git_head = original
    return code, calls


def test_the_operator_dispatch_refuses_before_any_lock(tmp_path, capsys) -> None:
    out, digest = made_handoff(tmp_path)
    good_env = tmp_path / "agi.env"
    good_env.write_text("CTX=gke-prod\n")
    other_env = tmp_path / "other.env"
    other_env.write_text("CTX=kind-local\n")
    cases = (({}, digest, "gke-prod", IDENTITY, "AGI_ENV must name"),                          # missing AGI_ENV
             ({"AGI_ENV": str(other_env)}, digest, "gke-prod", IDENTITY, "TARGET_MISMATCH"),     # another cluster
             ({"AGI_ENV": str(good_env)}, "0" * 64, "gke-prod", IDENTITY, "HANDOFF_DIGEST_MISMATCH"),
             ({"AGI_ENV": str(good_env)}, digest, "kind-local", IDENTITY, "WRONG_TARGET"),         # wrong context
             ({"AGI_ENV": str(good_env)}, digest, "gke-prod", None, "--production-identity"),      # no identity
             ({"AGI_ENV": str(good_env)}, digest, "gke-prod", "system:serviceaccount:ewallet-ci:jenkins",
              "IDENTITY_MISMATCH"))                                                               # not approved
    for env, d, context, identity, reason in cases:
        code, calls = dispatch(tmp_path, out, d, env, context, identity)
        output = capsys.readouterr().out
        assert (code, calls) == (2, []), output
        assert reason in output, (reason, output)


# --- release locks: renewal, expiry, ownership --------------------------------------------------------------
class Leases:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.version = 0
        self.fail_replace = False
        self.get_error: Exception | None = None      # e.g. HTTP 500/403 or a transport timeout
        self.replace_lost = False                     # the update is applied, its response is lost

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
        if self.get_error is not None:
            raise self.get_error
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
        updated = self._bump(document)
        if self.replace_lost:
            from scripts.ci.k8s_api import KubeTransportError
            raise KubeTransportError("PUT -> no response: TimeoutError")
        return updated


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

    from scripts.ci import deploy_identity

    identities: list = []
    monkeypatch.setattr(deploy_identity, "verify",
                        lambda approved, context, client: identities.append((approved, context, client.api)) or approved)
    monkeypatch.setattr(production_release, "kubectl_proxy", proxy)
    monkeypatch.setattr(production_release, "run", run)
    monkeypatch.setattr(release_dispatch, "_locks", lambda namespace, holder, api: Locks())
    monkeypatch.setattr(release_dispatch, "_git_head", lambda: SHA)
    monkeypatch.setenv("AGI_ENV", str(env_file))
    result = release_dispatch.main(["--plan", str(out / "release-plan.json"), "--execute", "--production-context",
                                    "gke-prod", "--handoff", str(out), "--handoff-sha256", digest,
                                    "--production-identity", IDENTITY, "--evidence-dir", str(tmp_path / "operator")])
    assert result == code
    # Identity was proven through the same proxy the locks use, before the transaction, then at each checkpoint.
    assert identities == [(IDENTITY, "gke-prod", "http://127.0.0.1:1")]
    assert seen["locks"] == ["production-release"] and seen["proxy_context"] == "gke-prod"
    seen["checkpoint"]()
    assert seen["renewed"] and len(identities) == 2  # each checkpoint renews the locks and re-proves identity
    assert seen["journal_path"].name == "transaction-journal.json"
    assert seen["journal_path"].parent.parent == tmp_path / "operator"
    assert seen["handoff"]["sha256"] == digest and seen["handoff"]["jenkins"]["build_number"] == "42"
    assert seen["verify"] is not None and seen["plan_sha"] == SHA
    verdict = json.loads((seen["journal_path"].parent / "release-verdict.json").read_text())
    assert verdict["verdict"] == state
    assert os.environ.get("PROMOTION_APPROVED") == "yes"  # set by the fixture; the dispatcher never sets it


# --- handoff semantics (Batch 2A.2) ------------------------------------------------------------------------------
@pytest.mark.parametrize(("change", "problem"), [
    (dict(status="FAILED", reason="NO_TESTS_EXECUTED"), "CI_NOT_PASSED"),
    (dict(status="NOT_APPLICABLE", reason="documentation-only change"), "CI_NOT_PASSED"),
    (dict(test_cases={"tests": 0, "failures": 0, "errors": 0, "skipped": 0}), "CI_NO_TESTS"),
    (dict(test_commands=0), "CI_NO_TESTS"),
    (dict(git_sha="d" * 40), "CI_WRONG_RELEASE"),
    (dict(git_sha=None), "CI_WRONG_RELEASE"),
    (dict(selected_components=["agents"]), "CI_UNTESTED_COMPONENTS"),
    (dict(scope_components=["rag-pipeline"], selected_components=["rag-pipeline"]), "CI_NOT_APPLICABLE"),
    (dict(executed=[{"stage": "tests", "kind": "test", "exit_code": 1, "classification": "TEST_FAILURE"}]),
     "CI_FAILED_COMMANDS"),
    (dict(test_cases={"tests": 10, "failures": 1, "errors": 0, "skipped": 0}), "CI_FAILED_TESTS"),
])
def test_the_ci_verdict_is_checked_semantically(tmp_path, change, problem) -> None:
    sources = handoff_sources(tmp_path)
    sources["ci-verdict.json"].write_text(json.dumps(ci_verdict(SHA, **change)))
    with pytest.raises(release_handoff.HandoffError, match=problem):
        release_handoff.build(tmp_path / "refused", sources)
    out, _ = made_handoff(tmp_path / "ok")
    digest = forged(out, "ci-verdict.json", lambda data: data.update(change))
    _, problems = release_handoff.verify(out, digest, head_sha=SHA, production_context="gke-prod",
                                         production_identity=IDENTITY)
    assert any(p.startswith(problem) for p in problems), problems


@pytest.mark.parametrize(("mutate", "problem"), [
    (lambda d: d["images"].pop("mcp-tools"), "SCAN_MISSING"),
    (lambda d: d["images"]["g3-byo-coordinator"].update(status="VULNERABLE"), "SCAN_FAILED"),
    (lambda d: d.update(verdict="SCANNER_ERROR"), "SCAN_NOT_PASSED"),
    (lambda d: d["images"]["mcp-tools"].update(digest_ref=d["images"]["mcp-tools"]["digest_ref"][:-1] + "0"),
     "SCAN_DIGEST_MISMATCH"),
    (lambda d: d.update(scanner="docker.io/aquasec/trivy:latest"), "SCAN_WRONG_SCANNER"),
    (lambda d: d.update(git_sha="d" * 40), "SCAN_WRONG_RELEASE"),
    (lambda d: d.update(verdict="NOT_APPLICABLE", images={}), "SCAN_NOT_PASSED"),  # a bypass, images exist
])
def test_the_image_scan_is_checked_against_the_plan_and_the_manifest(tmp_path, mutate, problem) -> None:
    out, _ = made_handoff(tmp_path)
    digest = forged(out, "image-scan.json", mutate)
    _, problems = release_handoff.verify(out, digest, head_sha=SHA, production_context="gke-prod",
                                         production_identity=IDENTITY)
    assert any(p.startswith(problem) for p in problems), problems


@pytest.mark.parametrize("mutate", [
    lambda h: h["units"][2].update(depends_on=[]),
    lambda h: h["units"][1].update(target_namespace="ewallet-agents"),
    lambda h: h["units"][3]["images"].update({"g3-byo-coordinator": "r.io/x/y@sha256:" + "1" * 64}),
    lambda h: h.update(release_branch="release/agent-fleet"),
])
def test_a_modified_unit_graph_in_the_handoff_is_refused(tmp_path, mutate) -> None:
    out, _ = made_handoff(tmp_path)
    digest = forged(out, "handoff.json", mutate)
    _, problems = release_handoff.verify(out, digest, head_sha=SHA, production_context="gke-prod",
                                         production_identity=IDENTITY)
    assert any(p.startswith("UNITS_MISMATCH") for p in problems), problems


def test_source_metadata_is_mandatory_and_must_be_well_formed(tmp_path) -> None:
    sources = handoff_sources(tmp_path)
    missing = dict(sources)
    missing.pop("source-metadata.txt")
    with pytest.raises(release_handoff.HandoffError, match="needs source-metadata.txt"):
        release_handoff.build(tmp_path / "a", missing)
    sources["source-metadata.txt"].write_text(sources["source-metadata.txt"].read_text().replace("e" * 64, "not-a-digest"))
    with pytest.raises(release_handoff.HandoffError, match="bundle_sha256"):
        release_handoff.build(tmp_path / "b", sources)
    branch = handoff_sources(tmp_path / "c")
    text = branch["source-metadata.txt"].read_text().replace("ref:refs/heads/release/coordinator", "ref:refs/heads/x")
    branch["source-metadata.txt"].write_text(text)
    with pytest.raises(release_handoff.HandoffError, match="does not carry release/coordinator"):
        release_handoff.build(tmp_path / "d", branch)


@pytest.mark.parametrize(("operator", "problem"), [
    (None, "IDENTITY_MISSING"), ("", "IDENTITY_MISSING"),
    ("system:serviceaccount:ewallet-ci:jenkins", "IDENTITY_MISMATCH"),
])
def test_the_operator_must_name_exactly_the_approved_principal(tmp_path, operator, problem) -> None:
    out, digest = made_handoff(tmp_path)
    _, problems = release_handoff.verify(out, digest, head_sha=SHA, production_context="gke-prod",
                                         production_identity=operator)
    assert any(problem in p for p in problems), problems


def test_a_handoff_never_approves_a_vague_identity(tmp_path) -> None:
    sources = handoff_sources(tmp_path)
    plan = json.loads(sources["release-plan.json"].read_text())
    sources["release-plan.json"].write_text(json.dumps({**plan, "production_identity": "ewallet-cicd-agent-release"}))
    with pytest.raises(release_handoff.HandoffError, match="exact Kubernetes principal"):
        release_handoff.build(tmp_path / "x", sources)


def test_the_ci_verdict_records_the_commit_it_tested(tmp_path) -> None:
    from scripts.ci.run_ci import write_verdict

    verdict = write_verdict(tmp_path, status="PASS", reason=None, scope_components=("agents",), selected=("agents",),
                            profile=None, results=[], git_sha=SHA)
    assert verdict["git_sha"] == SHA and json.loads((tmp_path / "ci-verdict.json").read_text())["git_sha"] == SHA


def test_the_scan_gate_records_the_scanned_digest_and_release(tmp_path) -> None:
    from datetime import date

    from scripts.ci.image_scan import gate

    ref = "asia-southeast1-docker.pkg.dev/p/r/mcp-tools@sha256:" + "d" * 64
    result = gate({"mcp-tools": {"status": "REPORT", "report": {"Results": []}, "image": ref}}, [], date(2026, 11, 18),
                  git_sha=SHA)
    assert result["verdict"] == "PASS" and result["git_sha"] == SHA and result["images"]["mcp-tools"]["digest_ref"] == ref


# --- Lease ownership: verified, lost, unknown (Batch 2A.2) -----------------------------------------------------
from scripts.ci.k8s_api import KubeTransportError  # noqa: E402
from scripts.ci.release_lock import LockOwnershipUnknown, OwnershipUncertain  # noqa: E402


def held_locks(api: Leases, clock: Clock, duration: int = 7200) -> ReleaseLocks:
    locks = ReleaseLocks(api, "ewallet-cicd", "build-1", duration_seconds=duration, clock=clock)
    locks.held["production-release"] = locks.acquire_one("production-release")
    return locks


@pytest.mark.parametrize(("break_it", "error"), [
    (lambda api: setattr(api, "get_error", KubeError("GET -> HTTP 500: internal error")), LockOwnershipUnknown),
    (lambda api: setattr(api, "get_error", KubeTransportError("GET -> no response: TimeoutError")), LockOwnershipUnknown),
    (lambda api: setattr(api, "get_error", KubeError("GET -> HTTP 403: forbidden")), LockOwnershipUnknown),
    (lambda api: setattr(api, "replace_lost", True), LockOwnershipUnknown),       # applied, response lost
    (lambda api: api.items.clear(), LockLost),                                       # disappeared
    (lambda api: api.items["release-lock-production-release"]["spec"].update(holderIdentity="build-2"), LockLost),
    (lambda api: api._bump(api.get("release-lock-production-release")), LockLost),  # resourceVersion changed
])
def test_lease_renewal_failures_are_lost_or_unknown_never_plain_errors(break_it, error) -> None:
    api, clock = Leases(), Clock()
    locks = held_locks(api, clock)
    break_it(api)
    with pytest.raises(error):
        locks.renew()


def test_after_a_lost_renewal_response_ownership_stays_unprovable() -> None:
    api, clock = Leases(), Clock()
    locks = held_locks(api, clock)
    api.replace_lost = True
    with pytest.raises(LockOwnershipUnknown):
        locks.renew()
    api.replace_lost = False
    with pytest.raises(LockLost, match="changed since"):  # the version this holder knows is stale now
        locks.renew()


def test_a_programming_error_is_not_turned_into_a_lock_state() -> None:
    api, clock = Leases(), Clock()
    locks = held_locks(api, clock)
    api.get_error = TypeError("bug")
    with pytest.raises(TypeError):
        locks.renew()


def test_lease_renewal_succeeds_and_expiry_is_lost() -> None:
    api, clock = Leases(), Clock()
    locks = held_locks(api, clock, duration=600)
    clock.now += timedelta(seconds=599)  # approaching expiry: still renewable
    locks.renew()
    clock.now += timedelta(seconds=601)  # beyond the renewed duration
    with pytest.raises(LockLost, match="expired"):
        locks.renew()


def test_a_transport_failure_of_the_api_client_is_a_kube_transport_error() -> None:
    from urllib.error import URLError

    from scripts.ci.k8s_api import Client

    def opener(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise URLError(TimeoutError("timed out"))

    with pytest.raises(KubeTransportError):
        Client("http://127.0.0.1:1", opener=opener).request("GET", "apis/coordination.k8s.io/v1/namespaces/x/leases/y")


@pytest.mark.parametrize("error", [LockOwnershipUnknown("LOCK_OWNERSHIP_UNKNOWN: GET -> HTTP 500"),
                                   LockOwnershipUnknown("LOCK_OWNERSHIP_UNKNOWN: GET -> no response")])
def test_uncertain_ownership_after_a_mutation_stops_everything_without_a_rollback(error) -> None:
    kube, helm = FakeKube(), FakeCluster()

    def checkpoint() -> None:
        if "llm-agent-mcp" in helm.upgraded:
            raise error

    report = transaction(kube, helm, checkpoint=checkpoint)
    assert report["verdict"] == "RECOVERY_REQUIRED" and report["lock_state"] == "LOCK_OWNERSHIP_UNKNOWN"
    assert not [c for c in helm.calls if c[:2] == ["helm", "rollback"]]
    assert helm.upgraded == {"llm-agent-mcp"}                      # no further Helm mutation
    assert not [c for c in kube.calls if c[0] == "patch"]           # no fleet mutation
    actions = [a["action"] for a in report["recovery"]["actions"]]
    assert actions[0] == "lock-and-identity-check" and "helm-observe-then-decide" in actions


def test_uncertain_ownership_during_verification_skips_the_rollback() -> None:
    kube, helm = FakeKube(), FakeCluster()

    def verify(expected):  # type: ignore[no-untyped-def]
        raise LockOwnershipUnknown("LOCK_OWNERSHIP_UNKNOWN: GET -> HTTP 503")

    report = transaction(kube, helm, verify=verify)
    assert report["verdict"] == "RECOVERY_REQUIRED" and report["verification"]["status"] == "NOT_RUN"
    assert not [c for c in helm.calls if c[:2] == ["helm", "rollback"]]
    assert len([c for c in kube.calls if c[0] == "patch"]) == 2      # the two promotions, no patch-back


def test_ownership_lost_just_before_the_rollback_skips_it() -> None:
    kube, helm = FakeKube(), FakeCluster()
    state = {"verified": False}

    def verify(expected):  # type: ignore[no-untyped-def]
        state["verified"] = True
        return False

    def checkpoint() -> None:
        if state["verified"]:
            raise LockLost("LOCK_LOST: release lock production-release expired before renewal")

    report = transaction(kube, helm, verify=verify, checkpoint=checkpoint)
    assert report["verdict"] == "RECOVERY_REQUIRED" and report["failure"] == "runtime verification failed after promotion"
    assert not [c for c in helm.calls if c[:2] == ["helm", "rollback"]]


def test_a_release_whose_lock_expires_during_a_long_upgrade_stops_after_it(monkeypatch) -> None:
    """Real ReleaseLocks as the checkpoint; the clock passes the lease duration inside the Coordinator upgrade."""

    api, clock = Leases(), Clock()
    locks = ReleaseLocks(api, "ewallet-cicd", "build-1", duration_seconds=1800, clock=clock)
    kube, helm = FakeKube(), FakeCluster()
    upgrade = helm.__call__

    def slow(command):  # type: ignore[no-untyped-def]
        if list(command[:3]) == ["helm", "upgrade", "llm-agent"]:
            clock.now += timedelta(seconds=1801)  # one step longer than the lease
        return upgrade(command)

    with locks.hold(["production-release"]):
        report = transaction(kube, FakeClusterProxy(helm, slow), checkpoint=locks.renew)
    assert report["verdict"] == "RECOVERY_REQUIRED" and report["lock_state"] == "LOCK_LOST"
    assert report["failed_unit"] == "coordinator-production"
    assert not [c for c in helm.calls if c[:2] == ["helm", "rollback"]]   # never races a possible new holder


def test_a_release_within_the_lease_renews_at_every_checkpoint() -> None:
    api, clock = Leases(), Clock()
    locks = ReleaseLocks(api, "ewallet-cicd", "build-1", duration_seconds=1800, clock=clock)
    kube, helm = FakeKube(), FakeCluster()
    upgrade = helm.__call__

    def slow(command):  # type: ignore[no-untyped-def]
        if list(command[:2]) == ["helm", "upgrade"]:
            clock.now += timedelta(seconds=1500)  # long, but each step stays inside one lease period
        return upgrade(command)

    renewals = {"n": 0}

    def checkpoint() -> None:
        renewals["n"] += 1
        locks.renew()

    with locks.hold(["production-release"]):
        report = transaction(kube, FakeClusterProxy(helm, slow), checkpoint=checkpoint)
    assert report["verdict"] == "DEPLOYED_PENDING_REGISTRY", report["failure"]
    assert renewals["n"] >= 8  # before and after every mutation


def test_runtime_verification_checks_ownership_before_every_helper_command(tmp_path) -> None:
    calls: list = []

    def checkpoint() -> None:
        calls.append("checkpoint")
        if calls.count("checkpoint") == 2:
            raise LockLost("LOCK_LOST: expired")

    def run(argv, env):  # type: ignore[no-untyped-def]
        calls.append(argv[1])
        return 0

    with pytest.raises(LockLost):
        runtime_verify.run_checks(["platform-status", "e2e-fraud"], tmp_path, run=run, environment={},
                                  checkpoint=checkpoint)
    assert calls == ["checkpoint", "status", "checkpoint"]  # the E2E never started


def test_a_hanging_helper_command_is_bounded_and_fails(monkeypatch, tmp_path) -> None:
    import subprocess

    def hang(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(runtime_verify.subprocess, "run", hang)
    assert runtime_verify._run(["agent_replay.sh", "e2e", "fraud"], {}) == runtime_verify.TIMED_OUT
    assert runtime_verify.HELPER_TIMEOUT < 7200 / 4  # far inside one lease period


# --- Helm ambiguous states (Batch 2A.2) --------------------------------------------------------------------------
from scripts.ci import promote  # noqa: E402
from scripts.ci import release_state as rs  # noqa: E402
from tests.ci.test_promote import OLD as HELM_OLD  # noqa: E402


def attempted(helm: FakeCluster, release: str = "llm-agent-mcp") -> promote.UnitPromotion:
    """A unit whose upgrade was attempted, with the real pre-mutation snapshot."""

    unit = promote.UnitPromotion("mcp-servers-production", release, "ewallet-mcp")
    unit.previous_revision = helm.revisions[release]
    unit.previous_manifest_sha256, unit.previous_images = promote.manifest_fingerprint(helm, release, "gke-prod")
    unit.status = "UPGRADE_ATTEMPTED"
    return unit


def move(helm: FakeCluster, release: str, state: str, image: str = MCP_NEW, bump: bool = True) -> None:
    if bump:
        helm.revisions[release] += 1
    helm.states[release] = state
    helm.images[release] = image


@pytest.mark.parametrize(("setup", "status"), [
    (lambda h: None, "NOT_CHANGED"),                                                     # unchanged, deployed
    (lambda h: move(h, "llm-agent-mcp", "pending-upgrade", HELM_OLD["mcp-tools"], bump=False), "STATE_UNKNOWN"),
    (lambda h: move(h, "llm-agent-mcp", "failed", HELM_OLD["mcp-tools"], bump=False), "STATE_UNKNOWN"),
    (lambda h: move(h, "llm-agent-mcp", "deployed", MCP_NEW, bump=False), "STATE_UNKNOWN"),  # same revision, new manifest
    (lambda h: move(h, "llm-agent-mcp", "deployed"), "ROLLED_BACK"),                     # advanced, deployed
    (lambda h: move(h, "llm-agent-mcp", "failed"), "ROLLED_BACK"),                       # advanced, failed
    (lambda h: move(h, "llm-agent-mcp", "pending-upgrade"), "STATE_UNKNOWN"),
    (lambda h: move(h, "llm-agent-mcp", "pending-rollback"), "STATE_UNKNOWN"),
    (lambda h: move(h, "llm-agent-mcp", "superseded"), "STATE_UNKNOWN"),
    (lambda h: h.history_error.add("llm-agent-mcp"), "STATE_UNKNOWN"),                    # history unavailable
])
def test_helm_rollback_decides_from_revision_status_and_manifest(setup, status) -> None:
    helm = FakeCluster()
    unit = attempted(helm)
    setup(helm)
    errors = promote.rollback(helm, [unit], "gke-prod")
    assert unit.status == status
    assert bool(errors) == (status not in {"NOT_CHANGED", "ROLLED_BACK"})
    rolled = [c for c in helm.calls if c[:2] == ["helm", "rollback"]]
    assert bool(rolled) == (status == "ROLLED_BACK")  # never a destructive retry from an ambiguous state


@pytest.mark.parametrize("break_it", [lambda h: setattr(h, "fail_rollback_of", "llm-agent-mcp"),     # nonzero
                                      lambda h: setattr(h, "rollback_state", "pending-rollback"),     # not deployed
                                      lambda h: setattr(h, "rollback_image", MCP_NEW)])               # wrong config
def test_a_rollback_must_end_deployed_with_the_recorded_configuration(break_it) -> None:
    helm = FakeCluster()
    unit = attempted(helm)
    move(helm, "llm-agent-mcp", "deployed")
    break_it(helm)
    errors = promote.rollback(helm, [unit], "gke-prod")
    assert unit.status == "ROLLBACK_FAILED" and errors


def test_a_promotion_never_starts_on_a_release_that_is_not_deployed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PROMOTION_APPROVED", "yes")
    helm = FakeCluster()
    helm.states["llm-agent-mcp"] = "pending-upgrade"  # someone else's operation is in flight
    unit = {"name": "mcp-servers-production", "kind": "helm-image-promotion",
            "helm": {"release": "llm-agent-mcp", "namespace": "ewallet-mcp", "image_values": {"mcp-tools": "mcp-tools"}},
            "images": {"mcp-tools": MCP_NEW}}
    record: list = []
    with pytest.raises(promote.PromotionError, match="not deployed"):
        promote.promote_unit(helm, unit, SHA, "gke-prod", tmp_path, True, record)
    assert record[0].status == "REFUSED" and not [c for c in helm.calls if c[:2] == ["helm", "upgrade"]]


def test_the_snapshot_never_stores_values_or_secrets(tmp_path) -> None:
    helm = FakeCluster()
    unit = attempted(helm)
    assert len(unit.previous_manifest_sha256) == 64 and unit.previous_images == [HELM_OLD["mcp-tools"]]
    assert set(json.loads(json.dumps(unit.__dict__))) == {"name", "release", "namespace", "previous_revision", "status",
                                                          "detail", "previous_manifest_sha256", "previous_images"}


# --- deployment identity (Batch 2A.2) ----------------------------------------------------------------------------
from scripts.ci import deploy_identity  # noqa: E402


class IdentityClient:
    def __init__(self, responses: dict) -> None:
        self.responses, self.api, self.calls = responses, "http://127.0.0.1:1", []

    def request(self, method, path, body=None, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((method, path, body))
        response = self.responses.get(path.split("/")[2])
        if isinstance(response, Exception):
            raise response
        return response


def review(name: str | None) -> dict:
    return {"kind": "SelfSubjectReview", "status": {"userInfo": {"username": name, "groups": ["system:authenticated"]}}}


def whoami(name: str | None):  # type: ignore[no-untyped-def]
    return lambda command: json.dumps(review(name))


def test_the_authenticated_principal_must_be_the_approved_one_on_both_views() -> None:
    client = IdentityClient({"v1": review(IDENTITY)})
    assert deploy_identity.verify(IDENTITY, "gke-prod", client, run=whoami(IDENTITY)) == IDENTITY
    method, path, body = client.calls[0]
    assert (method, path) == ("POST", "apis/authentication.k8s.io/v1/selfsubjectreviews")  # read-only review
    assert body["kind"] == "SelfSubjectReview"


def test_a_1_27_cluster_is_reviewed_through_v1beta1() -> None:
    client = IdentityClient({"v1": KubeError("POST -> HTTP 404: not found"), "v1beta1": review(IDENTITY)})
    assert deploy_identity.api_principal(client) == IDENTITY


@pytest.mark.parametrize(("client", "run", "code"), [
    (IdentityClient({"v1": review("system:serviceaccount:ewallet-ci:jenkins")}), whoami(IDENTITY), "IDENTITY_MISMATCH"),
    (IdentityClient({"v1": review(IDENTITY)}), whoami("someone@example.com"), "IDENTITY_MISMATCH"),  # context switched
    (IdentityClient({"v1": KubeError("POST -> HTTP 401: Unauthorized")}), whoami(IDENTITY), "IDENTITY_UNAVAILABLE"),
    (IdentityClient({"v1": KubeError("POST -> HTTP 404"), "v1beta1": KubeError("POST -> HTTP 404")}), whoami(IDENTITY),
     "IDENTITY_UNAVAILABLE"),
    (IdentityClient({"v1": {"status": {}}}), whoami(IDENTITY), "IDENTITY_MALFORMED"),
    (IdentityClient({"v1": review(IDENTITY)}), lambda command: "not json", "IDENTITY_UNAVAILABLE"),
    (IdentityClient({"v1": review(IDENTITY)}), whoami(None), "IDENTITY_MALFORMED"),
])
def test_identity_failures_refuse(client, run, code) -> None:
    with pytest.raises(deploy_identity.IdentityError, match=code):
        deploy_identity.verify(IDENTITY, "gke-prod", client, run=run)


def test_kubectl_whoami_is_bound_to_the_explicit_context_and_fails_closed() -> None:
    seen: list = []

    def run(command):  # type: ignore[no-untyped-def]
        seen.append(command)
        return json.dumps(review(IDENTITY))

    assert deploy_identity.kubectl_principal("gke-prod", run) == IDENTITY
    assert seen == [["kubectl", "--context", "gke-prod", "auth", "whoami", "-o", "json"]]
    for context in ("", "in-cluster"):  # never the Jenkins identity, never the current context
        with pytest.raises(deploy_identity.IdentityError):
            deploy_identity.kubectl_principal(context, run)


@pytest.mark.parametrize(("approved", "operator", "code"), [
    ("", IDENTITY, "IDENTITY_NOT_APPROVED"), ("ewallet-cicd-agent-release", "ewallet-cicd-agent-release", "IDENTITY_NOT_APPROVED"),
    (IDENTITY, None, "IDENTITY_MISSING"), (IDENTITY, "system:serviceaccount:ewallet-ci:other", "IDENTITY_MISMATCH"),
])
def test_the_approved_identity_contract(approved, operator, code) -> None:
    with pytest.raises(deploy_identity.IdentityError, match=code):
        deploy_identity.require_approved(approved, operator)
    assert deploy_identity.require_approved(IDENTITY, IDENTITY) == IDENTITY


def test_an_identity_change_during_execution_stops_without_a_rollback() -> None:
    kube, helm = FakeKube(), FakeCluster()

    def checkpoint() -> None:
        if "llm-agent-mcp" in helm.upgraded:
            raise deploy_identity.IdentityError("IDENTITY_MISMATCH: the production context authenticates as "
                                                "{'kubeconfig': 'someone@example.com'}")

    report = transaction(kube, helm, checkpoint=checkpoint)
    assert report["verdict"] == "RECOVERY_REQUIRED" and report["lock_state"] == "IDENTITY_UNVERIFIED"
    assert not [c for c in helm.calls if c[:2] == ["helm", "rollback"]]
    assert isinstance(deploy_identity.IdentityError("x"), OwnershipUncertain)


# --- Batch 2A.3 P0: no candidate cleanup without mutation authority ------------------------------------------
from scripts.ci.release_lock import LockOwnershipUnknown as _Unknown  # noqa: E402


def deletes(kube: FakeKube) -> list:
    return [c for c in kube.calls if c[0] == "delete"]


def creates(kube: FakeKube) -> list:
    return [c for c in kube.calls if c[0] == "create"]


def after_creates(error: Exception, kube: FakeKube):  # type: ignore[no-untyped-def]
    """A checkpoint that fails once both candidates exist (readiness done, before E2E)."""

    def checkpoint() -> None:
        if len(creates(kube)) == 2:
            raise error
    return checkpoint


def candidate_records(report: dict) -> dict:
    return {a["name"]: a for a in report["recovery"]["actions"] if a["action"] == "candidate-recovery"}


@pytest.mark.parametrize(("error", "state"), [
    (LockLost("LOCK_LOST: release lock production-release is now held by build-2"), "LOCK_LOST"),            # Case A
    (_Unknown("LOCK_OWNERSHIP_UNKNOWN: GET -> HTTP 503"), "LOCK_OWNERSHIP_UNKNOWN"),                           # Case B
    (deploy_identity.IdentityError("IDENTITY_MISMATCH: someone@example.com"), "IDENTITY_UNVERIFIED"),          # Case C
])
def test_ownership_unproven_after_candidate_creation_issues_no_cleanup_delete(tmp_path, error, state) -> None:
    kube, helm = FakeKube(), FakeCluster()
    report = transaction(kube, helm, checkpoint=after_creates(error, kube), journal=tmp_path / "j.json")
    assert len(creates(kube)) == 2 and deletes(kube) == []          # nothing after the failure
    assert not [c for c in kube.calls if c[0] == "patch"]
    assert not [c for c in helm.calls if c[:2] in (["helm", "upgrade"], ["helm", "rollback"])]
    assert report["verdict"] == "RECOVERY_REQUIRED" and report["lock_state"] == state
    assert str(error) in report["failure"]                           # the original error, not CANDIDATE_ERROR
    assert "CANDIDATE_ERROR" not in report["failure"]
    records = candidate_records(report)
    assert set(records) == {"feature-rag-specialist-cicd", "drift-specialist-cicd"}
    for record in records.values():
        assert record["uid"] and record["execution_id"] == EXEC and record["release_sha"] == SHA
        assert record["label"] == "ewallet.io/cicd-execution" and record["cleanup_status"] == "SKIPPED_OWNERSHIP_UNPROVEN"
        assert record["skipped_reason"] == str(error) and record["last_known_state"].startswith("Accepted=True")
        assert len(record["prerequisites"]) == 2 and "never by name" in record["rule"]
    assert report["recovery"]["actions"][0]["action"] == "lock-and-identity-check"
    journal = json.loads((tmp_path / "j.json").read_text())
    skipped = [e for e in journal["events"] if e["event"] == "candidate-cleanup-skipped"]
    assert len(skipped) == 1 and len(skipped[0]["candidates"]) == 2


def test_ownership_lost_during_cleanup_stops_after_the_authorized_delete() -> None:                          # Case D
    kube = FakeKube()
    created = {}
    for agent in ("feature-rag-specialist", "drift-specialist"):
        item = kube.create(sa_render(agent))
        created[item["metadata"]["name"]] = item["metadata"]["uid"]
    allowed = {"n": 0}

    def checkpoint() -> None:
        allowed["n"] += 1
        if allowed["n"] > 1:
            raise LockLost("LOCK_LOST: expired")

    with pytest.raises(sandbox_candidate.CleanupHalted) as halted:
        sandbox_candidate.cleanup(kube, CONTRACT, created, EXEC, checkpoint=checkpoint, sleep=lambda s: None)
    assert len(deletes(kube)) == 1                                   # the second DELETE was never sent
    remaining = {r["name"]: r["cleanup_status"] for r in halted.value.remaining}
    first, second = list(created)
    assert remaining == {first: "DELETE_REQUESTED", second: "SKIPPED_OWNERSHIP_UNPROVEN"} or \
        remaining == {second: "SKIPPED_OWNERSHIP_UNPROVEN"}
    assert isinstance(halted.value.cause, LockLost)


def sa_render(agent: str) -> dict:
    from scripts.ci import sandbox_agents

    return sandbox_agents.render_candidate(CONTRACT, agent, NEW, SHA, EXEC)


def test_ownership_lost_during_cleanup_after_an_ordinary_failure_in_the_transaction(tmp_path) -> None:
    kube, helm = FakeKube(), FakeCluster()
    wrong = lambda rid: specialist_reply("feature-rag-specialist", rid, fraud_score={"fraud_probability": 0.9})  # noqa: E731

    def checkpoint() -> None:
        if deletes(kube):                                            # after the first cleanup DELETE
            raise _Unknown("LOCK_OWNERSHIP_UNKNOWN: GET -> no response")

    report = transaction(kube, helm, checkpoint=checkpoint, post=fake_post({"feature-rag-specialist": wrong}),
                         journal=tmp_path / "j.json")
    assert len(deletes(kube)) == 1
    assert report["verdict"] == "RECOVERY_REQUIRED" and report["lock_state"] == "LOCK_OWNERSHIP_UNKNOWN"
    records = candidate_records(report)
    assert len(records) == 2 and {r["cleanup_status"] for r in records.values()} == {"DELETE_REQUESTED",
                                                                                     "SKIPPED_OWNERSHIP_UNPROVEN"}


def test_an_ordinary_candidate_failure_with_verified_ownership_still_cleans_up() -> None:                    # Case E
    kube, helm = FakeKube(behavior="pending"), FakeCluster()
    report = transaction(kube, helm, checkpoint=lambda: None)
    assert report["verdict"] == "FAILED_NOTHING_CHANGED" and "NOT_READY" in report["failure"]
    assert len(deletes(kube)) == 2
    assert not [k for k in kube.objects if k[0] in ("SandboxAgent", "ActorTemplate") and "-cicd" in k[2]]
    assert not report["recovery"]["recovery_required"]


def test_a_failing_cleanup_with_verified_ownership_is_bounded_and_never_passes() -> None:                   # Case F
    kube, helm = FakeKube(delete_fails=True), FakeCluster()
    report = transaction(kube, helm, checkpoint=lambda: None)
    attempts = len(deletes(kube))
    assert 2 < attempts < 200                                        # retried, but bounded by the cleanup deadline
    assert report["verdict"] == "RECOVERY_REQUIRED" and report["candidate_cleanup_incomplete"]
    assert report["units"]["candidates"][0]["status"] == "CLEANUP_INCOMPLETE"
    assert not [c for c in helm.calls if c[:2] == ["helm", "upgrade"]]


def test_a_replaced_candidate_is_reported_and_never_deleted() -> None:                                       # Case G
    kube = FakeKube()
    created = kube.create(sa_render("drift-specialist"))
    del kube.objects[("SandboxAgent", "ewallet-sandbox", "drift-specialist-cicd")]
    kube._admit(sandbox_candidate.sa.render_candidate(CONTRACT, "drift-specialist", NEW, SHA, "20261118t111111-ffffff"),
                ready=True)
    state, remaining = sandbox_candidate.cleanup(kube, CONTRACT, {"drift-specialist-cicd": created["metadata"]["uid"]},
                                                 EXEC, checkpoint=lambda: None, sleep=lambda s: None)
    assert state == {"drift-specialist-cicd": "NOT_OURS_LEFT_UNTOUCHED"} and remaining == [] and deletes(kube) == []


def test_the_recovery_plan_for_a_failed_cleanup_is_deterministic_and_issues_nothing(tmp_path) -> None:      # Case H
    kube, helm = FakeKube(), FakeCluster()
    report = transaction(kube, helm, checkpoint=after_creates(LockLost("LOCK_LOST: x"), kube), journal=tmp_path / "j.json")
    journal = json.loads((tmp_path / "j.json").read_text())
    calls_before = (list(kube.calls), list(helm.calls))
    first, second = production_release.recovery_plan(journal), production_release.recovery_plan(journal)
    assert first == second == report["recovery"]
    assert (kube.calls, helm.calls) == calls_before                  # planning never touches a cluster
    assert first["recovery_required"]
    text = json.dumps(first)
    assert "token" not in text.lower() and "password" not in text.lower()


# --- Batch 2A.3 P1: one Helm recovery policy -----------------------------------------------------------------
PREVIOUS = {"revision": 19, "manifest_sha256": "a" * 64, "images": ["r.io/p/mcp-tools@sha256:" + "a" * 64]}
CHANGED = {"manifest_sha256": "b" * 64, "images": ["r.io/p/mcp-tools@sha256:" + "b" * 64]}
SAME = {"manifest_sha256": PREVIOUS["manifest_sha256"], "images": PREVIOUS["images"]}


def helm_journal(**event) -> dict:  # type: ignore[no-untyped-def]
    attempt = {"event": "helm-upgrade-attempted", "unit": "mcp-servers-production", "release": "llm-agent-mcp",
               "namespace": "ewallet-mcp", "previous_revision": 19, "previous_status": "deployed",
               "previous_manifest_sha256": PREVIOUS["manifest_sha256"], "previous_images": PREVIOUS["images"],
               "target_images": ["r.io/p/mcp-tools@sha256:" + "b" * 64]}
    attempt.update(event)
    return {"state": "STARTED", "git_sha": SHA, "execution_id": EXEC, "events": [attempt]}


def helm_step(journal: dict, latest, current, after_rollback: bool = False) -> dict:  # type: ignore[no-untyped-def]
    observed = {"llm-agent-mcp": {"latest": latest, "current": current, "after_rollback": after_rollback}}
    plan = production_release.recovery_plan(journal, observed)
    return next(a for a in plan["actions"] if a.get("release") == "llm-agent-mcp")


@pytest.mark.parametrize(("latest", "current", "after", "decision", "action"), [
    ({"revision": 19, "status": "deployed"}, SAME, False, "NOT_CHANGED", "none-helm-unchanged"),            # 1
    ({"revision": 19, "status": "deployed"}, CHANGED, False, "STATE_UNKNOWN", "helm-manual-inspection"),    # 2
    ({"revision": 19, "status": "pending-upgrade"}, SAME, False, "STATE_UNKNOWN", "helm-manual-inspection"),  # 3
    ({"revision": 19, "status": "failed"}, SAME, False, "STATE_UNKNOWN", "helm-manual-inspection"),         # 4
    ({"revision": 20, "status": "deployed"}, CHANGED, False, "ROLLBACK", "helm-guarded-rollback"),          # 5
    ({"revision": 20, "status": "failed"}, CHANGED, False, "ROLLBACK", "helm-guarded-rollback"),            # 6
    ({"revision": 20, "status": "pending-upgrade"}, CHANGED, False, "STATE_UNKNOWN", "helm-manual-inspection"),  # 7
    ({"revision": 20, "status": "pending-rollback"}, CHANGED, False, "STATE_UNKNOWN", "helm-manual-inspection"),  # 8
    (None, None, False, "STATE_UNKNOWN", "helm-manual-inspection"),                                          # 9
    ({"revision": 24, "status": "deployed"}, CHANGED, False, "STATE_UNKNOWN", "helm-manual-inspection"),    # 12
    ({"revision": 18, "status": "deployed"}, SAME, False, "STATE_UNKNOWN", "helm-manual-inspection"),       # 12
    ({"revision": 21, "status": "deployed"}, CHANGED, True, "STATE_UNKNOWN", "helm-manual-inspection"),     # 13
    ({"revision": 21, "status": "deployed"}, {**SAME, "images": CHANGED["images"]}, True, "STATE_UNKNOWN",
     "helm-manual-inspection"),                                                                              # 14
    ({"revision": 21, "status": "deployed"}, SAME, True, "RESTORED", "none-helm-restored"),
    ({"revision": 21, "status": "deployed"}, SAME, False, "RESTORED", "none-helm-restored"),                # helm --atomic
    ({"revision": 20, "status": "deployed"}, None, False, "STATE_UNKNOWN", "helm-manual-inspection"),       # manifest unreadable
])
def test_the_recovery_plan_applies_the_hardened_helm_policy(latest, current, after, decision, action) -> None:
    step = helm_step(helm_journal(), latest, current, after)
    assert (step["decision"], step["action"]) == (decision, action)
    assert step["prerequisites"] == list(production_release.PREREQUISITES)
    if decision == "ROLLBACK":
        assert step["command"].startswith("helm rollback llm-agent-mcp 19 ")
        assert "only RESTORED" in step["then"] and "exit code alone does not" in step["then"]
    else:
        assert "command" not in step  # never an unconditional rollback


@pytest.mark.parametrize("missing", ["previous_manifest_sha256", "previous_images"])                          # 10, 11
def test_a_journal_without_the_fingerprint_gets_manual_inspection_only(missing) -> None:
    journal = helm_journal()
    del journal["events"][0][missing]
    for observed in (None, {"llm-agent-mcp": {"latest": {"revision": 20, "status": "deployed"}, "current": CHANGED}}):
        step = next(a for a in production_release.recovery_plan(journal, observed)["actions"] if a.get("release"))
        assert step["action"] == "helm-manual-inspection" and "command" not in step


def test_without_observations_the_plan_asks_to_observe_with_the_same_rules() -> None:
    step = next(a for a in production_release.recovery_plan(helm_journal())["actions"] if a.get("release"))
    assert step["action"] == "helm-observe-then-decide" and step["rules"] == list(promote.RECOVERY_RULES)
    assert any("observe-helm" in command for command in step["observe"]) and "command" not in step


def test_the_journal_captures_the_fingerprint_before_the_upgrade(tmp_path) -> None:                          # 15, 16
    kube, helm = FakeKube(), FakeCluster()
    pre_digest, pre_images = promote.manifest_fingerprint(helm, "llm-agent-mcp", "gke-prod")
    original = helm.__call__

    class Interrupted(BaseException):
        pass

    def crash_in_upgrade(command):  # type: ignore[no-untyped-def]
        result = original(command)  # the upgrade is applied ...
        if list(command[:3]) == ["helm", "upgrade", "llm-agent-mcp"]:
            raise Interrupted()     # ... and the process dies before it learns the result
        return result

    journal = tmp_path / "j.json"
    with pytest.raises(Interrupted):
        transaction(kube, FakeClusterProxy(helm, crash_in_upgrade), journal=journal)
    data = json.loads(journal.read_text())
    attempt = next(e for e in data["events"] if e["event"] == "helm-upgrade-attempted")
    assert attempt["previous_manifest_sha256"] == pre_digest and attempt["previous_images"] == pre_images
    assert attempt["previous_status"] == "deployed" and attempt["previous_revision"] == 19
    assert attempt["target_images"] == [MCP_NEW]
    latest, current = promote.observe(helm, "llm-agent-mcp", "ewallet-mcp", "gke-prod")  # read-only
    step = next(a for a in production_release.recovery_plan(data, {"llm-agent-mcp": {"latest": latest, "current": current}})
                ["actions"] if a.get("release") == "llm-agent-mcp")
    assert step["decision"] == "ROLLBACK"  # revision 20, deployed, not the recorded manifest


@pytest.mark.parametrize("state", ["deployed", "failed", "pending-upgrade", "pending-rollback", "superseded"])
def test_automated_rollback_and_recovery_plan_reach_the_same_decision(state) -> None:
    helm = FakeCluster()
    unit = attempted(helm)
    move(helm, "llm-agent-mcp", state)
    latest, current = promote.observe(helm, "llm-agent-mcp", "ewallet-mcp", "gke-prod")
    journal = helm_journal(previous_revision=unit.previous_revision, previous_manifest_sha256=unit.previous_manifest_sha256,
                           previous_images=unit.previous_images)
    planned = helm_step(journal, latest, current)["decision"]
    promote.rollback(helm, [unit], "gke-prod")
    automated = {"ROLLED_BACK": "ROLLBACK", "NOT_CHANGED": "NOT_CHANGED", "STATE_UNKNOWN": "STATE_UNKNOWN"}[unit.status]
    assert planned == automated


def test_a_transient_ownership_failure_still_stops_all_later_cleanup() -> None:
    """Once unproven, ownership is not re-acquired by a later successful check: an API blip at the E2E gate
    must not be followed by cleanup DELETEs even though the next checkpoint would pass."""

    kube, helm = FakeKube(), FakeCluster()
    failed = {"once": False}

    def checkpoint() -> None:
        if len(creates(kube)) == 2 and not failed["once"]:
            failed["once"] = True
            raise _Unknown("LOCK_OWNERSHIP_UNKNOWN: GET -> HTTP 500 (transient)")

    report = transaction(kube, helm, checkpoint=checkpoint)
    assert failed["once"] and deletes(kube) == []
    assert report["verdict"] == "RECOVERY_REQUIRED" and len(candidate_records(report)) == 2
