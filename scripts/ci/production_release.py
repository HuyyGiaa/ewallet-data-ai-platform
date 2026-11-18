"""One production release transaction across the Agent Platform release-unit kinds.

A verified production plan lists its units in dependency order. Each kind has one executor:

* ``sandbox-candidate``     scripts/ci/sandbox_candidate.py (creates, verifies and always deletes the
                            candidate SandboxAgents; it changes nothing that needs a rollback)
* ``helm-image-promotion``  scripts/ci/promote.py (image-only ``helm upgrade --atomic``)
* ``fleet-image-promotion`` scripts/ci/fleet_promote.py (JSON patch of the SandboxAgent image)

Fail-closed rules:

* ``execute`` needs a runtime verifier; without one the transaction refuses before any mutation.
* Every mutation is preceded by a lock checkpoint (``ReleaseLocks.renew``) and a durable journal entry
  (``transaction-journal.json``, written atomically) naming the unit, the previous revision or image and
  the target, so an interrupted process leaves a deterministic recovery plan
  (``python -m scripts.ci.production_release recovery-plan --journal <file>``).
* The first failure stops the release, including an exception raised by the verifier. Every unit this
  run may have changed (attempted, not only confirmed) is rolled back in reverse order; the original
  failure stays the reported cause and each rollback error is listed.
* Ownership is re-proven before and after every mutation and before any rollback. When it cannot be
  proven - confirmed loss (``LockLost``), an unverifiable Lease (``LockOwnershipUnknown``: API error,
  timeout, lost renewal response) or a changed deployment identity (``deploy_identity.IdentityError``)
  - no further mutation runs, NOT EVEN an automatic rollback: another release may hold the lock, or the
  credentials are not the approved ones. Recovery is by the journal, by hand (``RECOVERY_REQUIRED``).
* A Helm release left in an ambiguous state (pending, unreadable, unchanged revision but not deployed or
  with another manifest) is ``STATE_UNKNOWN``: no automated action, ``RECOVERY_REQUIRED``.

Lifecycle states (``verdict``):

    CHECKED_NOT_EXECUTED       no approval or no --execute; nothing changed
    FAILED_NOTHING_CHANGED     failed before any production mutation (candidate cleaned up)
    FAILED_ROLLED_BACK         failed after mutations; every one was rolled back and verified
    ROLLBACK_FAILED            a rollback failed or could not be proven safe (critical)
    RECOVERY_REQUIRED          lock lost, candidate cleanup incomplete, or state unknown (critical)
    DEPLOYED_PENDING_REGISTRY  runtime verified; Agent Registry publication pending its own approval
    RELEASE_COMPLETE           runtime verified and nothing registered changed (or reconciled later by
                               scripts/ci/registry_release.py)

Nothing changes without ``execute`` and ``PROMOTION_APPROVED=yes``; production credentials are never
implied (an explicit kube context is required and must not be the in-cluster Jenkins identity).
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping, Sequence

from scripts.ci import fleet_promote, promote, registry_release, sandbox_candidate
from scripts.ci import release_state as rs
from scripts.ci.release_lock import OwnershipUncertain

SUCCESS = frozenset({"RELEASE_COMPLETE", "DEPLOYED_PENDING_REGISTRY"})
CRITICAL = frozenset({"ROLLBACK_FAILED", "RECOVERY_REQUIRED"})


class ProductionReleaseError(RuntimeError):
    """The transaction cannot start, or a unit failed."""


@contextlib.contextmanager
def kubectl_proxy(context: str):  # type: ignore[no-untyped-def]
    """``kubectl --context <production> proxy`` on a free loopback port; yields its URL.

    The CD API client (scripts/ci/k8s_api.py) then keeps its create-only, uid-precondition and
    JSON-patch semantics under the explicit production identity, never the in-cluster Jenkins token.
    """

    import re
    import subprocess

    if not context or context == "in-cluster":
        raise ProductionReleaseError("an explicit production kube context is required")
    process = subprocess.Popen(["kubectl", "--context", context, "proxy", "--port", "0", "--address", "127.0.0.1"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        line = process.stdout.readline() if process.stdout else ""
        match = re.search(r"127\.0\.0\.1:([0-9]+)", line)
        if not match:
            raise ProductionReleaseError("kubectl proxy did not start for the production context")
        yield f"http://127.0.0.1:{match.group(1)}"
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)


Verify = Callable[[Mapping[str, Any]], bool]


class Journal:
    """Durable, append-only record of a production transaction (atomic rewrite with fsync per event).

    It is the recovery contract after a crash: which units were attempted, what may have changed,
    the previous revisions and images, and the verification state. ``None`` path = in memory (tests).
    """

    def __init__(self, path: Path | None, header: Mapping[str, Any]) -> None:
        self.path = path
        self.data: dict[str, Any] = {"schema": 1, **header, "events": [], "state": "STARTED"}
        self._flush()

    def event(self, name: str, /, **fields: Any) -> None:
        self.data["events"].append({"event": name, "at": datetime.now(timezone.utc).isoformat(), **fields})
        self._flush()

    def finish(self, state: str) -> None:
        self.data["state"] = state
        self._flush()

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)


def recovery_plan(journal: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic recovery actions from a journal (read-only; an operator executes them by hand).

    A mutation counts as possibly applied from the moment it was journaled as attempted until a
    later event proves it rolled back, unchanged or verified.
    """

    actions: list[dict[str, Any]] = []
    events = list(journal.get("events") or [])
    state = journal.get("state")
    closed = {(e.get("unit"), e.get("target")) for e in events
              if e.get("event") in {"rolled-back", "not-changed"}}
    verified = state in SUCCESS  # the verified runtime confirms every attempted mutation
    candidates: dict[str, dict[str, Any]] = {}
    for item in events:
        kind = item.get("event")
        if kind == "helm-upgrade-attempted" and not verified and (item["unit"], item["release"]) not in closed:
            actions.append({"action": "helm-inspect-then-rollback", "release": item["release"],
                            "namespace": item["namespace"], "previous_revision": item["previous_revision"],
                            "rule": "if the latest revision is not previous_revision: helm rollback <release> "
                                    "<previous_revision> --wait, then confirm the latest revision is deployed"})
        elif kind == "fleet-patch-attempted" and not verified and (item["unit"], item["agent"]) not in closed:
            actions.append({"action": "sandboxagent-restore-image", "namespace": "ewallet-sandbox", "name": item["agent"],
                            "uid": item["uid"], "previous_image": item["previous_image"],
                            "target_image": item["target_image"],
                            "rule": "only if uid and spec.byo.deployment.image still equal uid/target_image: JSON patch "
                                    "(test uid, test target_image, replace previous_image); otherwise a concurrent "
                                    "change - do not overwrite, escalate"})
        elif kind == "candidate-created":
            candidates[item["name"]] = item
        elif kind == "candidates-deleted":
            for name in item.get("names", []):
                candidates.pop(name, None)
        elif kind == "candidate-snapshot-prefix":
            actions.append({"action": "gcs-remove-exact-prefix", "prefix": item["prefix"],
                            "rule": "remove objects under exactly this prefix only (owner cleanup, never a parent "
                                    "prefix and never a fleet campaign prefix)"})
    for name, item in sorted(candidates.items()):
        actions.append({"action": "delete-candidate", "namespace": "ewallet-sandbox", "name": name, "uid": item.get("uid"),
                        "execution_id": journal.get("execution_id"),
                        "rule": "delete with a uid precondition only if the object still has this uid and the "
                                "ewallet.io/cicd-execution label; then confirm its ActorTemplate is gone"})
    for item in events:
        if item.get("event") == "ownership-unproven":
            actions.insert(0, {"action": "lock-and-identity-check", "state": item.get("state"),
                               "rule": "before anything else: read the release Leases in ewallet-cicd and the "
                                       "authenticated principal; act only after proving no other release holds "
                                       "the lock and the credentials are the approved identity"})
    if state not in {"RELEASE_COMPLETE", "DEPLOYED_PENDING_REGISTRY", "FAILED_ROLLED_BACK", "FAILED_NOTHING_CHANGED",
                     "CHECKED_NOT_EXECUTED"}:
        actions.append({"action": "verify-runtime", "rule": "run agent_replay.sh status and the E2E cases against the "
                        "recovered state before any new release"})
    if state == "DEPLOYED_PENDING_REGISTRY":
        actions.append({"action": "registry-publication", "rule": "follow registry.procedure of the release verdict "
                        "under its own approval"})
    # Only the snapshot prefix remains after a clean run; it is an owner cleanup, not a recovery.
    critical = [a for a in actions if a["action"] not in {"gcs-remove-exact-prefix", "registry-publication"}]
    return {"state": state, "git_sha": journal.get("git_sha"), "recovery_required": bool(critical),
            "actions": actions}


def expectations(plan: Mapping[str, Any], helm: list[promote.UnitPromotion], revisions: Mapping[str, int],
                 fleet: list[fleet_promote.AgentPromotion]) -> dict[str, Any]:
    """What the runtime must show after this transaction (input to runtime_verify reconciliation)."""

    images: dict[str, str] = {}
    for unit in plan["units"]:
        if unit["kind"] == "helm-image-promotion" and unit["helm"]["release"] == "llm-agent":
            images["ewallet-agents/Deployment/fraud-coordinator"] = unit["images"]["g3-byo-coordinator"]
        elif unit["kind"] == "helm-image-promotion" and unit["helm"]["release"] == "llm-agent-mcp":
            for server in sandbox_candidate.MCP_SERVERS:
                images[f"ewallet-mcp/Deployment/{server}"] = unit["images"]["mcp-tools"]
        elif unit["kind"] == "fleet-image-promotion":
            for agent in ("feature-rag-specialist", "drift-specialist"):
                images[f"ewallet-sandbox/SandboxAgent/{agent}"] = unit["images"]["g3-byo-coordinator"]
    return {"git_sha": plan["git_sha"], "helm_revisions": dict(revisions), "images": images,
            "upgraded": [item.release for item in helm if item.status == "UPGRADED"],
            "patched": [item.agent for item in fleet if item.status == "PROMOTED"]}


def run(plan: Mapping[str, Any], context: str, manifest: Mapping[str, Any], *, execute: bool, kube: Any,
        helm_runner: promote.Runner = rs.run_command, verify: Verify | None = None,
        forward: Callable[[], contextlib.AbstractContextManager[str]] | None = None,
        evidence: Path | None = None, checkpoint: Callable[[], None] | None = None,
        journal_path: Path | None = None, execution_id: str | None = None,
        handoff: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if plan.get("stage") != "production":
        raise ProductionReleaseError("only a production plan runs the production transaction")
    if not context or context == "in-cluster":
        raise ProductionReleaseError("an explicit production kube context is required")
    if execute and verify is None:
        raise ProductionReleaseError("a production release with --execute needs its runtime verifier; refused before "
                                     "any mutation")
    # One approval for every production mutation: the candidate SandboxAgents, the fleet patch and Helm
    # (promote.py reads the same variable). Without it every unit is checked, nothing is changed.
    approved = execute and os.environ.get("PROMOTION_APPROVED") == "yes"
    execution_id = execution_id or sandbox_candidate.sa.new_execution_id()
    journal = Journal(journal_path, {"git_sha": plan["git_sha"], "release_branch": plan.get("release_branch"),
                                     "context": context, "execution_id": execution_id, "approved": approved,
                                     "handoff_sha256": (handoff or {}).get("sha256"),
                                     "units": [u["name"] for u in plan["units"]]})
    helm: list[promote.UnitPromotion] = []
    fleet: list[fleet_promote.AgentPromotion] = []
    candidates: list[dict[str, Any]] = []
    revisions: dict[str, int] = {}
    failure, failed_unit, lock_lost, lock_state = "", None, False, None
    forward = forward or (lambda: sandbox_candidate.kubectl_forward(context, sandbox_candidate.sa.load_contract()))
    check = checkpoint or (lambda: None)
    # Baseline of both Helm releases (read-only): a release this run does not upgrade must stay at it.
    try:
        revisions.update({release: promote.current_revision(helm_runner, release, namespace, context)
                          for release, namespace in sorted(rs.RELEASE_NAMESPACE.items())})
    except (promote.PromotionError, rs.ReleaseStateError, ValueError) as error:
        journal.finish("FAILED_NOTHING_CHANGED")
        raise ProductionReleaseError(f"cannot read the Helm baseline: {error}") from error
    journal.event("baseline", helm_revisions=dict(revisions))

    def helm_attempt(unit_name: str) -> Callable[[promote.UnitPromotion], None]:
        def record(item: promote.UnitPromotion) -> None:
            try:
                check()
            except BaseException:
                item.status = "NOT_STARTED"  # helm never ran
                raise
            journal.event("helm-upgrade-attempted", unit=unit_name, release=item.release, namespace=item.namespace,
                          previous_revision=item.previous_revision)
        return record

    def fleet_attempt(unit_name: str) -> Callable[[fleet_promote.AgentPromotion], None]:
        def record(item: fleet_promote.AgentPromotion) -> None:
            journal.event("fleet-patch-attempted", unit=unit_name, agent=item.agent, uid=item.uid,
                          previous_image=item.previous_image, target_image=item.target_image)
        return record

    with tempfile.TemporaryDirectory(prefix="production-release.") as directory:
        for unit in plan["units"]:
            try:
                check()
                journal.event("unit-started", unit=unit["name"], kind=unit["kind"])
                if unit["kind"] == "sandbox-candidate":
                    contract = sandbox_candidate.sa.load_contract()
                    journal.event("candidate-snapshot-prefix", unit=unit["name"],
                                  prefix=sandbox_candidate.sa.snapshot_prefix(contract, plan["git_sha"], execution_id))
                    report = sandbox_candidate.run(
                        kube, unit, plan, manifest, execute=approved, forward=forward, execution_id=execution_id,
                        checkpoint=check,
                        emit=lambda event, unit_name=unit["name"]: journal.event(event.pop("event"), unit=unit_name,
                                                                                **event),
                        evidence=(evidence / unit["name"]) if evidence else None)
                    candidates.append({k: v for k, v in report.__dict__.items() if k != "ownership_error"})
                    if report.ownership_error is not None:
                        raise report.ownership_error  # cleaned up already; stop as an ownership failure
                    if report.status not in {"PASS", "CHECKED_NOT_EXECUTED"}:
                        raise ProductionReleaseError(f"{unit['name']}: {report.failure_code}: "
                                                     f"{'; '.join(report.problems[:3])}")
                elif unit["kind"] == "helm-image-promotion":
                    item = promote.promote_unit(helm_runner, unit, plan["git_sha"], context, Path(directory), approved,
                                                helm, before_mutation=helm_attempt(unit["name"]))
                    if item.status == "UPGRADED":
                        revisions[item.release] = promote.current_revision(helm_runner, item.release, item.namespace,
                                                                           context)
                        journal.event("helm-upgraded", unit=unit["name"], release=item.release,
                                      revision=revisions[item.release])
                        check()  # the upgrade may have run up to 25 minutes: prove ownership again
                elif unit["kind"] == "fleet-image-promotion":
                    fleet_promote.promote(kube, unit, execute=approved, record=fleet,
                                          before_mutation=fleet_attempt(unit["name"]), checkpoint=check)
                    check()  # after the last SandboxAgent readiness wait
                else:
                    raise ProductionReleaseError(f"{unit['name']}: kind {unit['kind']} is not a production executor")
                journal.event("unit-finished", unit=unit["name"])
            except OwnershipUncertain as error:
                failure, failed_unit, lock_lost, lock_state = str(error), unit["name"], True, error.state
                break
            except Exception as error:  # every failure, typed or not, enters the same recovery path
                failure, failed_unit = (str(error) if isinstance(error, (ProductionReleaseError, promote.PromotionError,
                                                                         rs.ReleaseStateError,
                                                                         fleet_promote.FleetPromotionError))
                                        else f"{unit['name']}: {type(error).__name__}: {str(error)[:300]}"), unit["name"]
                break
        expected = expectations(plan, helm, revisions, fleet)
        verification: dict[str, Any] = {"status": "NOT_RUN"}
        if not failure and approved:
            try:
                check()
                journal.event("verification-started", expected=expected)
                passed = verify(expected) if verify is not None else False
                verification = {"status": "PASS" if passed is True else "FAIL"}
                if passed is not True:
                    failure = "runtime verification failed after promotion"
            except OwnershipUncertain as error:
                failure, lock_lost, lock_state = str(error), True, error.state
                verification = {"status": "NOT_RUN", "reason": error.state}
            except Exception as error:  # a verifier crash is a failed verification, never a pass
                failure = f"runtime verification raised {type(error).__name__}: {str(error)[:300]}"
                verification = {"status": "ERROR", "error": failure}
            journal.event("verification-finished", **verification)
    rollback_errors: list[str] = []
    if failure and not lock_lost:
        # A rollback is a production mutation too: it runs only while ownership and identity still hold.
        try:
            check()
        except OwnershipUncertain as error:
            lock_lost, lock_state = True, error.state
            journal.event("rollback-skipped", reason=str(error), state=error.state)
        except Exception as error:  # an unexpected checkpoint failure: ownership unproven, fail closed
            lock_lost, lock_state = True, "LOCK_OWNERSHIP_UNKNOWN"
            journal.event("rollback-skipped", reason=f"{type(error).__name__}: {error}", state=lock_state)
    if failure and not lock_lost:
        journal.event("rollback-started", failure=failure)
        contract = sandbox_candidate.sa.load_contract()
        # Reverse plan order: the Coordinator, then the fleet, then MCP. Rollbacks never raise; any
        # unexpected exception is still recorded as a rollback error.
        for unit in reversed(plan["units"]):
            try:
                if unit["kind"] == "helm-image-promotion":
                    mine = [i for i in helm if i.name == unit["name"]]
                    rollback_errors += promote.rollback(helm_runner, mine, context)
                    for i in mine:
                        if i.status in {"ROLLED_BACK", "NOT_CHANGED"}:
                            journal.event("rolled-back" if i.status == "ROLLED_BACK" else "not-changed",
                                          unit=unit["name"], target=i.release)
                elif unit["kind"] == "fleet-image-promotion":
                    rollback_errors += fleet_promote.rollback(kube, contract, fleet)
                    for i in fleet:
                        if i.status in {"ROLLED_BACK", "NOT_CHANGED"}:
                            journal.event("rolled-back" if i.status == "ROLLED_BACK" else "not-changed",
                                          unit=unit["name"], target=i.agent)
            except Exception as error:
                rollback_errors.append(f"{unit['name']}: rollback raised {type(error).__name__}: {str(error)[:200]}")
    changed = (any(i.status in {"UPGRADED", "ROLLED_BACK", "ROLLBACK_FAILED"} for i in helm)
               or any(i.status in {"PATCHED", "PROMOTED", "ROLLED_BACK", "ROLLBACK_FAILED", "ROLLBACK_NOT_READY"}
                      for i in fleet))
    cleanup_incomplete = [r for report in candidates for r in report.get("cleanup_incomplete") or []]
    registry: dict[str, Any] = {"status": "NOT_APPLICABLE"}
    state_unknown = [i.name for i in helm if i.status == "STATE_UNKNOWN"]
    if lock_lost or cleanup_incomplete or state_unknown:
        state = "RECOVERY_REQUIRED"
    elif rollback_errors:
        state = "ROLLBACK_FAILED"
    elif failure:
        # Attempts that provably changed nothing (NOT_CHANGED) do not count as a rolled-back change.
        state = "FAILED_ROLLED_BACK" if changed else "FAILED_NOTHING_CHANGED"
    elif not approved:
        state = "CHECKED_NOT_EXECUTED"
    else:
        registry = registry_release.requirement(
            plan, coordinator_changed=any(i.release == "llm-agent" and i.status == "UPGRADED" for i in helm),
            fleet_changed=any(i.status == "PROMOTED" for i in fleet))
        state = "RELEASE_COMPLETE" if registry["status"] == "NOT_REQUIRED" else "DEPLOYED_PENDING_REGISTRY"
    if lock_lost:
        journal.event("ownership-unproven", state=lock_state, failure=failure)
    journal.finish(state)
    created = any(report.get("cleanup") for report in candidates)
    return {
        "verdict": state, "stage": "production", "git_sha": plan["git_sha"], "execution_id": execution_id,
        "production_changed": changed,
        # Candidates are additive objects in the production namespace, removed before the verdict.
        "candidate_objects_created": created, "candidate_cleanup_incomplete": cleanup_incomplete,
        "failure": failure or None, "failed_unit": failed_unit, "lock_lost": lock_lost, "lock_state": lock_state,
        "state_unknown": state_unknown,
        "rollback_errors": rollback_errors, "verification": verification, "expected_runtime": expected,
        "registry": registry, "recovery": recovery_plan(journal.data),
        "units": {"candidates": candidates, "helm": [i.__dict__ for i in helm], "fleet": [asdict(i) for i in fleet]},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("recovery-plan", help="read-only: the recovery actions a journal requires")
    plan.add_argument("--journal", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        journal = json.loads(args.journal.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        parser.error(f"cannot read the journal: {error}")
    result = recovery_plan(journal)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["recovery_required"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
