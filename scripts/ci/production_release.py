"""One production release transaction across the Agent Platform release-unit kinds.

A verified production plan lists its units in dependency order. Each kind has one executor:

* ``sandbox-candidate``     scripts/ci/sandbox_candidate.py (creates, verifies and always deletes the
                            candidate SandboxAgents; it changes nothing that needs a rollback)
* ``helm-image-promotion``  scripts/ci/promote.py (image-only ``helm upgrade --atomic``)
* ``fleet-image-promotion`` scripts/ci/fleet_promote.py (JSON patch of the SandboxAgent image)

The first failure stops the release. Every unit this run changed is then rolled back in reverse
order (Helm to the recorded revision, SandboxAgents to the recorded image); the original failure
stays the reported cause and each rollback error is listed. Runtime verification runs after the
last promotion and is reconciled with this transaction (scripts/ci/runtime_verify.py): the expected
Helm revisions are the ones this run produced, and the running images must be the plan digests.

Nothing changes without ``execute``; production credentials are never implied (an explicit kube
context is required and must not be the in-cluster Jenkins identity).
"""

from __future__ import annotations

import contextlib
from dataclasses import asdict
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping

from scripts.ci import fleet_promote, promote, sandbox_candidate
from scripts.ci import release_state as rs


class ProductionReleaseError(RuntimeError):
    """The transaction cannot start."""


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
REGISTRY_FOLLOWUP = (
    "Agent Registry entries carry the Coordinator BYO image digest and the SandboxAgent fleet image digest as "
    "compared labels (ops/llm_agent/registry_catalog.py), so a promotion makes the registry stale until it is "
    "republished. Publication is a production write with its own approval; CI/CD never publishes.")


def registry_followup(plan: Mapping[str, Any], helm: list[promote.UnitPromotion],
                      fleet: list[fleet_promote.AgentPromotion]) -> dict[str, Any]:
    """What the owner must run (under its own approval) after a promotion that changed a registered image."""

    coordinator = any(i.release == "llm-agent" and i.status == "UPGRADED" for i in helm)
    sandbox = any(i.status == "PROMOTED" for i in fleet)
    if not coordinator and not sandbox:
        return {"status": "UNCHANGED", "reason": "no registered image changed"}
    fleet_image = next((u["images"]["g3-byo-coordinator"] for u in plan["units"]
                        if u["kind"] == "fleet-image-promotion"), "<live fleet image>")
    common = ["--values-overlay", "infra/helm/llm-agent-platform/g3/g3.yaml",
              "--values-overlay", "infra/helm/llm-agent-platform/agi/agents.yaml",
              "--values-overlay", "<live llm-agent values>", "--sandbox-image", fleet_image,
              "--git-sha", plan["git_sha"]]
    return {"status": "PUBLICATION_REQUIRES_APPROVAL", "reason": REGISTRY_FOLLOWUP,
            "changed": {"coordinator": coordinator, "sandbox_fleet": sandbox},
            "commands": [["python", "-m", "ops.llm_agent.registry_catalog", "publish", "--registry", "<registry>",
                          "--dry-run", *common],
                         ["python", "-m", "ops.llm_agent.registry_catalog", "publish", "--registry", "<registry>",
                          "--confirm-live-write", "<registry>", *common],
                         ["scripts/llm_agent/agent_replay.sh", "registry"]]}


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
            "patched": [item.agent for item in fleet if item.status in {"PATCHED", "PROMOTED"}]}


def run(plan: Mapping[str, Any], context: str, manifest: Mapping[str, Any], *, execute: bool, kube: Any,
        helm_runner: promote.Runner = rs.run_command, verify: Verify | None = None,
        forward: Callable[[], contextlib.AbstractContextManager[str]] | None = None,
        evidence: Path | None = None) -> dict[str, Any]:
    if plan.get("stage") != "production":
        raise ProductionReleaseError("only a production plan runs the production transaction")
    if not context or context == "in-cluster":
        raise ProductionReleaseError("an explicit production kube context is required")
    # One approval for every production mutation: the candidate SandboxAgents, the fleet patch and Helm
    # (promote.py reads the same variable). Without it every unit is checked, nothing is changed.
    approved = execute and os.environ.get("PROMOTION_APPROVED") == "yes"
    helm: list[promote.UnitPromotion] = []
    fleet: list[fleet_promote.AgentPromotion] = []
    candidates: list[dict[str, Any]] = []
    revisions: dict[str, int] = {}
    failure, failed_unit = "", None
    forward = forward or (lambda: sandbox_candidate.kubectl_forward(context, sandbox_candidate.sa.load_contract()))
    # Baseline of both Helm releases (read-only): a release this run does not upgrade must stay at it.
    try:
        revisions.update({release: promote.current_revision(helm_runner, release, namespace, context)
                          for release, namespace in sorted(rs.RELEASE_NAMESPACE.items())})
    except (promote.PromotionError, rs.ReleaseStateError, ValueError) as error:
        raise ProductionReleaseError(f"cannot read the Helm baseline: {error}") from error
    with tempfile.TemporaryDirectory(prefix="production-release.") as directory:
        for unit in plan["units"]:
            try:
                if unit["kind"] == "sandbox-candidate":
                    report = sandbox_candidate.run(kube, unit, plan, manifest, execute=approved, forward=forward,
                                                   evidence=(evidence / unit["name"]) if evidence else None)
                    candidates.append(report.__dict__)
                    if report.status not in {"PASS", "CHECKED_NOT_EXECUTED"}:
                        raise ProductionReleaseError(f"{unit['name']}: {report.failure_code}: "
                                                     f"{'; '.join(report.problems[:3])}")
                elif unit["kind"] == "helm-image-promotion":
                    item = promote.promote_unit(helm_runner, unit, plan["git_sha"], context, Path(directory), execute,
                                                helm)
                    if item.status == "UPGRADED":
                        revisions[item.release] = promote.current_revision(helm_runner, item.release, item.namespace,
                                                                           context)
                elif unit["kind"] == "fleet-image-promotion":
                    fleet_promote.promote(kube, unit, execute=approved, record=fleet)
                else:
                    raise ProductionReleaseError(f"{unit['name']}: kind {unit['kind']} is not a production executor")
            except (ProductionReleaseError, promote.PromotionError, rs.ReleaseStateError,
                    fleet_promote.FleetPromotionError) as error:
                failure, failed_unit = str(error), unit["name"]
                break
            except Exception as error:  # an API failure is still a failed unit that must be rolled back
                failure, failed_unit = f"{unit['name']}: {type(error).__name__}: {str(error)[:300]}", unit["name"]
                break
        expected = expectations(plan, helm, revisions, fleet)
        if not failure and approved and verify is not None and not verify(expected):
            failure = "runtime verification failed after promotion"
    rollback_errors: list[str] = []
    if failure:
        # Reverse plan order: the Coordinator, then the fleet, then MCP.
        for unit in reversed(plan["units"]):
            if unit["kind"] == "helm-image-promotion":
                rollback_errors += promote.rollback(helm_runner, [i for i in helm if i.name == unit["name"]], context)
            elif unit["kind"] == "fleet-image-promotion":
                rollback_errors += fleet_promote.rollback(kube, sandbox_candidate.sa.load_contract(), fleet)
    changed = (any(i.status in {"UPGRADED", "ROLLED_BACK"} for i in helm)
               or any(i.status not in {"PENDING", "ALREADY_CURRENT", "CHECKED_NOT_EXECUTED", "REFUSED"} for i in fleet))
    created = any(report.get("cleanup") for report in candidates)
    verdict = "FAIL" if failure else ("PASS" if approved else "CHECKED_NOT_EXECUTED")
    if rollback_errors:
        verdict = "ROLLBACK_FAILED"
    return {
        "verdict": verdict, "stage": "production", "git_sha": plan["git_sha"], "production_changed": changed,
        # Candidates are additive objects in the production namespace, removed before the verdict.
        "candidate_objects_created": created,
        "failure": failure or None, "failed_unit": failed_unit, "rollback_errors": rollback_errors,
        "expected_runtime": expected,
        "registry": registry_followup(plan, helm, fleet) if not failure else {"status": "NOT_APPLICABLE"},
        "units": {"candidates": candidates, "helm": [i.__dict__ for i in helm], "fleet": [asdict(i) for i in fleet]},
    }
