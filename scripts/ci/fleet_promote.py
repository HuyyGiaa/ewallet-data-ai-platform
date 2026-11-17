"""Release unit kind ``fleet-image-promotion``: image-only promotion of the two production SandboxAgents.

The production fleet (``ewallet-sandbox/feature-rag-specialist`` and ``drift-specialist``) is not
Helm-managed and must not be migrated into a release (frozen platform, owner decision D3). This
promoter therefore changes exactly one field per SandboxAgent, ``spec.byo.deployment.image``, with
an RFC 6902 JSON patch that first tests the object's uid and current image, so a concurrent change
or a recreated object fails the patch instead of being overwritten. Ownership, labels, the WorkerPool,
ServiceAccounts, credentials and every other field are untouched.

Per agent, in a fixed order (Feature-RAG, then Drift):

1. read and record uid, generation and current image (the rollback target), before any mutation;
2. the agent must be healthy at its current image (``assess_sandbox_agent``), else ``REFUSED``;
3. current image == release digest: ``ALREADY_CURRENT`` (nothing is patched);
4. patch, then wait for Accepted/Ready at the new generation and an ActorTemplate with the new
   image and a golden actor (kagent re-snapshots the actor);
5. on any failure, every agent this run patched is patched back (test op on the new image) and
   waited Ready, in reverse order; rollback errors are reported, never hidden.

Nothing is patched without ``execute`` (and the caller's production approval).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Callable, Mapping

from scripts.ci import sandbox_agents as sa
from scripts.ci.sandbox_candidate import Kube

IMAGE_PATH = "/spec/byo/deployment/image"


class FleetPromotionError(RuntimeError):
    """A fleet promotion step failed."""


@dataclass
class AgentPromotion:
    agent: str
    uid: str | None = None
    previous_image: str | None = None
    previous_generation: int | None = None
    target_image: str | None = None
    status: str = "PENDING"
    detail: list[str] = field(default_factory=list)


def _image(item: Mapping[str, Any]) -> str | None:
    return (((item.get("spec") or {}).get("byo") or {}).get("deployment") or {}).get("image")


def wait_healthy(kube: Kube, contract: Mapping[str, Any], agent: str, image: str, *, timeout: float,
                 clock: Callable[[], float], sleep: Callable[[float], None]) -> list[str]:
    namespace = contract["namespace"]
    deadline = clock() + timeout
    while True:
        item = kube.get("SandboxAgent", namespace, agent) or {}
        problems = sa.assess_sandbox_agent(item, image=image, name=agent)
        template, _ = sa.assess_actor_template(kube.list("ActorTemplate", namespace), item, image=image,
                                               module=contract["agents"][agent]["module"])
        if not problems and not template:
            return []
        if sa.conditions(item).get("Accepted", {}).get("status") == "False" or clock() >= deadline:
            return problems + template
        sleep(10)


def promote_agent(kube: Kube, contract: Mapping[str, Any], agent: str, image: str, *, execute: bool,
                  record: list[AgentPromotion], clock: Callable[[], float], sleep: Callable[[float], None]) -> AgentPromotion:
    namespace = contract["namespace"]
    result = AgentPromotion(agent, target_image=image)
    record.append(result)
    item = kube.get("SandboxAgent", namespace, agent)
    if item is None:
        result.status = "REFUSED"
        raise FleetPromotionError(f"production SandboxAgent {agent} is missing")
    result.uid = item["metadata"].get("uid")
    result.previous_image = _image(item)
    result.previous_generation = item["metadata"].get("generation")
    if (item.get("metadata") or {}).get("labels", {}).get(sa.CANDIDATE_LABEL):
        result.status = "REFUSED"
        raise FleetPromotionError(f"{agent} is a CI/CD candidate, not the production SandboxAgent")
    health = sa.assess_sandbox_agent(item, image=result.previous_image or "", name=agent)
    if health:
        result.status, result.detail = "REFUSED", health
        raise FleetPromotionError(f"{agent} is not healthy before promotion: {'; '.join(health[:3])}")
    if result.previous_image == image:
        result.status = "ALREADY_CURRENT"
        return result
    if not execute:
        result.status = "CHECKED_NOT_EXECUTED"
        return result
    kube.patch("SandboxAgent", namespace, agent, [
        {"op": "test", "path": "/metadata/uid", "value": result.uid},
        {"op": "test", "path": IMAGE_PATH, "value": result.previous_image},
        {"op": "replace", "path": IMAGE_PATH, "value": image},
    ])
    result.status = "PATCHED"
    problems = wait_healthy(kube, contract, agent, image, timeout=contract["timeouts"]["ready_seconds"],
                            clock=clock, sleep=sleep)
    if problems:
        result.detail = problems
        raise FleetPromotionError(f"{agent} did not become Ready on the release image: {'; '.join(problems[:3])}")
    result.status = "PROMOTED"
    return result


def rollback(kube: Kube, contract: Mapping[str, Any], promoted: list[AgentPromotion], *,
             clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Patch every changed agent back to its recorded image, in reverse order; returns errors (never raises)."""

    errors = []
    for item in reversed(promoted):
        if item.status not in {"PATCHED", "PROMOTED"} or not item.previous_image:
            continue
        try:
            kube.patch("SandboxAgent", contract["namespace"], item.agent, [
                {"op": "test", "path": "/metadata/uid", "value": item.uid},
                {"op": "test", "path": IMAGE_PATH, "value": item.target_image},
                {"op": "replace", "path": IMAGE_PATH, "value": item.previous_image},
            ])
            problems = wait_healthy(kube, contract, item.agent, item.previous_image,
                                    timeout=contract["timeouts"]["ready_seconds"], clock=clock, sleep=sleep)
            if problems:
                errors.append(f"{item.agent}: rolled back but not Ready: {'; '.join(problems[:3])}")
                item.status = "ROLLBACK_NOT_READY"
            else:
                item.status = "ROLLED_BACK"
        except Exception as error:  # report every rollback failure; keep going
            errors.append(f"{item.agent}: rollback failed: {type(error).__name__}: {str(error)[:200]}")
            item.status = "ROLLBACK_FAILED"
    return errors


def promote(kube: Kube, unit: Mapping[str, Any], *, execute: bool, record: list[AgentPromotion],
            contract: Mapping[str, Any] | None = None, clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep) -> list[AgentPromotion]:
    """Promote both agents; on failure the caller (production_release) rolls ``record`` back."""

    contract = contract or sa.load_contract()
    if unit.get("target_namespace") != contract["namespace"]:
        raise FleetPromotionError(f"fleet promotion targets {unit.get('target_namespace')}, not {contract['namespace']}")
    image = unit["images"]["g3-byo-coordinator"]
    for agent in sa.production_agents(contract)[::-1]:  # feature-rag-specialist, then drift-specialist
        promote_agent(kube, contract, agent, image, execute=execute, record=record, clock=clock, sleep=sleep)
    return record
