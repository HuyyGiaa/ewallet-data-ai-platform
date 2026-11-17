"""SandboxAgent deployment contract, readiness and runtime reconciliation for Agent CI/CD.

The Feature-RAG and Drift Specialists run only as BYO SandboxAgents (``kagent.dev/v1alpha2``) on
the one shared WorkerPool ``ewallet-sandbox/business-agents`` (``ate.dev/v1alpha1``, gVisor). That
runtime is frozen, so CI/CD adapts to it and never re-models it:

* ``render_candidate`` renders a release-scoped candidate from the frozen fleet template
  (``scripts/experiments/render_sandbox.py``) and changes exactly four things: the name
  (``<agent>-cicd``), the labels (release SHA and the unique execution id), the image digest and the
  snapshot prefix (``gs://ate-snapshots/ewallet-sandbox/cicd-<sha12>/<execution>/<candidate>/``, unique
  per execution so a retry of the same SHA never restores an earlier golden snapshot). ``contract_differences``
  proves nothing else moved (command, module, ServiceAccount, credential Secret, egress proxy,
  resources, security context, WorkerPool reference, allowed domains).
* ``assess_*`` decide readiness from the objects the API returns: Accepted and Ready at the
  current ``observedGeneration``, the expected image and WorkerPool, a gVisor WorkerPool with
  workers, and one ActorTemplate controlled by the SandboxAgent with the same image and module, a
  gVisor class, the WorkerPool selector and a golden actor (the Substrate actor snapshot).
* ``reconcile`` states the release identity: expected SHA = artifact provenance (image manifest) =
  plan SHA = what is deployed (label and image digest). Any mismatch is a named problem.

Nothing here talks to a cluster; ``scripts/ci/sandbox_candidate.py`` and
``scripts/ci/fleet_promote.py`` pass in the objects they read.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "jenkins/cd/sandbox-candidate.contract.json"
NAMESPACE = "ewallet-sandbox"
WORKER_POOL = "business-agents"
SANDBOX_CLASS = "gvisor"
WORKER_SELECTOR = {"kagent.dev/worker-pool": WORKER_POOL}
POOL_REF = {"apiGroup": "ate.dev", "kind": "WorkerPool", "name": WORKER_POOL}
CANDIDATE_LABEL = "ewallet.io/cicd-candidate"
RELEASE_SHA_LABEL = "ewallet.io/release-sha"
CANDIDATE_OF_LABEL = "ewallet.io/candidate-of"
EXECUTION_LABEL = "ewallet.io/cicd-execution"
# <UTC second>-<random>, e.g. 20261118t101500-3fa9c2: unique per run, a valid label value and GCS path part.
EXECUTION_ID = re.compile(r"^[0-9]{8}t[0-9]{6}-[0-9a-f]{6}$")
MANAGED_BY = "ewallet-jenkins-cd"
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST_REF = re.compile(r"^[a-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$")
# The frozen template needs an AGI campaign id; this placeholder is replaced and must never survive.
_PLACEHOLDER_CAMPAIGN = "agi-00000000T000000Z"


class SandboxContractError(ValueError):
    """A candidate cannot be rendered within the frozen SandboxAgent contract."""


def load_contract(path: Path = CONTRACT) -> dict[str, Any]:
    contract = json.loads(path.read_text(encoding="utf-8"))
    if contract.get("namespace") != NAMESPACE or contract.get("worker_pool") != WORKER_POOL:
        raise SandboxContractError("the candidate contract must target ewallet-sandbox/business-agents")
    if contract.get("sandbox_class") != SANDBOX_CLASS:
        raise SandboxContractError("the candidate contract must require gVisor")
    if set(contract.get("agents", {})) != {"feature-rag-specialist", "drift-specialist"}:
        raise SandboxContractError("the candidate contract must name exactly the two production Specialists")
    return contract


def production_agents(contract: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(contract["agents"]))


def credential_secret(contract: Mapping[str, Any], agent: str) -> str:
    return contract["agents"][agent]["credential_secret"].format(credential_release=contract["credential_release"])


def frozen_documents(image: str, credential_release: str) -> dict[str, dict[str, Any]]:
    """The frozen fleet's SandboxAgents by name, exactly as render_sandbox renders them."""

    from scripts.experiments.render_sandbox import render

    text = render("fleet", image, _PLACEHOLDER_CAMPAIGN, credential_release)
    return {doc["metadata"]["name"]: doc for doc in yaml.safe_load_all(text)
            if doc and doc.get("kind") == "SandboxAgent"}


def candidate_name(contract: Mapping[str, Any], agent: str) -> str:
    return f"{agent}{contract['candidate_suffix']}"


def new_execution_id(now: datetime | None = None) -> str:
    """A fresh execution id per candidate run (never derived from the SHA alone)."""

    import secrets
    from datetime import timezone

    moment = now or datetime.now(timezone.utc)
    return f"{moment.strftime('%Y%m%dt%H%M%S')}-{secrets.token_hex(3)}"


def snapshot_prefix(contract: Mapping[str, Any], git_sha: str, execution_id: str) -> str:
    """The exact GCS prefix this execution's candidates write; cleanup may remove this prefix only."""

    return f"{contract['snapshot_prefix']}{git_sha[:12]}/{execution_id}/"


def render_candidate(contract: Mapping[str, Any], agent: str, image: str, git_sha: str,
                     execution_id: str) -> dict[str, Any]:
    if agent not in contract["agents"]:
        raise SandboxContractError(f"{agent} is not a production Specialist")
    if not SHA.fullmatch(git_sha):
        raise SandboxContractError("the release SHA must be a full lowercase SHA")
    if not EXECUTION_ID.fullmatch(execution_id):
        raise SandboxContractError("the execution id must be <yyyymmdd>t<hhmmss>-<6 hex>")
    if not DIGEST_REF.fullmatch(image):
        raise SandboxContractError("the candidate image must be an immutable digest reference")
    frozen = frozen_documents(image, contract["credential_release"])[agent]
    name = candidate_name(contract, agent)
    document = copy.deepcopy(frozen)
    document["metadata"] = {
        "name": name, "namespace": NAMESPACE,
        "labels": {CANDIDATE_LABEL: "true", CANDIDATE_OF_LABEL: agent, RELEASE_SHA_LABEL: git_sha,
                   EXECUTION_LABEL: execution_id, "app.kubernetes.io/managed-by": MANAGED_BY},
        "annotations": {"ewallet.io/candidate-contract": "jenkins/cd/sandbox-candidate.contract.json"},
    }
    document["spec"]["substrate"]["snapshotsConfig"]["location"] = (
        f"{snapshot_prefix(contract, git_sha, execution_id)}{name}/")
    problems = contract_differences(document, frozen)
    if problems:
        raise SandboxContractError("; ".join(problems))
    if _PLACEHOLDER_CAMPAIGN in json.dumps(document):
        raise SandboxContractError("the placeholder campaign leaked into the candidate")
    return document


def _strip(document: Mapping[str, Any]) -> dict[str, Any]:
    spec = copy.deepcopy(dict(document.get("spec") or {}))
    spec.get("byo", {}).get("deployment", {}).pop("image", None)
    spec.get("substrate", {}).get("snapshotsConfig", {}).pop("location", None)
    return spec


APPROVED_LABELS = frozenset({CANDIDATE_LABEL, CANDIDATE_OF_LABEL, RELEASE_SHA_LABEL, EXECUTION_LABEL,
                             "app.kubernetes.io/managed-by"})


def contract_differences(candidate: Mapping[str, Any], frozen: Mapping[str, Any]) -> list[str]:
    """Everything but name, labels, image and snapshot prefix must equal the frozen fleet entry."""

    problems: list[str] = []
    labels = (candidate.get("metadata") or {}).get("labels") or {}
    if set(labels) != APPROVED_LABELS or labels.get(CANDIDATE_LABEL) != "true":
        problems.append(f"the candidate labels must be exactly {sorted(APPROVED_LABELS)}")
    image = ((((candidate.get("spec") or {}).get("byo") or {}).get("deployment") or {}).get("image")) or ""
    if not DIGEST_REF.fullmatch(image):
        problems.append("the candidate image must be an immutable digest reference")
    if candidate.get("apiVersion") != "kagent.dev/v1alpha2" or candidate.get("kind") != "SandboxAgent":
        problems.append("the candidate must be a kagent.dev/v1alpha2 SandboxAgent")
    if (candidate.get("metadata") or {}).get("namespace") != NAMESPACE:
        problems.append(f"the candidate must live in {NAMESPACE}")
    name = (candidate.get("metadata") or {}).get("name", "")
    if name == (frozen.get("metadata") or {}).get("name"):
        problems.append("a candidate must never reuse a production SandboxAgent name")
    if _strip(candidate) != _strip(frozen):
        problems.append("the candidate spec differs from the frozen fleet template beyond image and snapshot prefix")
    location = (((candidate.get("spec") or {}).get("substrate") or {}).get("snapshotsConfig") or {}).get("location", "")
    if not re.fullmatch(r"gs://ate-snapshots/ewallet-sandbox/cicd-[0-9a-f]{12}/[0-9]{8}t[0-9]{6}-[0-9a-f]{6}/"
                        + re.escape(name) + "/", location):
        problems.append("the candidate snapshot prefix must be cicd-<sha12>/<execution>/<candidate>/, never a fleet campaign")
    return problems


# --- readiness ---------------------------------------------------------------------------------------------------
def conditions(item: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {c.get("type"): c for c in ((item.get("status") or {}).get("conditions") or []) if isinstance(c, Mapping)}


def assess_sandbox_agent(item: Mapping[str, Any] | None, *, image: str, name: str) -> list[str]:
    if not item:
        return [f"SandboxAgent {name} is missing"]
    problems: list[str] = []
    generation = (item.get("metadata") or {}).get("generation")
    status = item.get("status") or {}
    found = conditions(item)
    for kind in ("Accepted", "Ready"):
        condition = found.get(kind)
        if condition is None:
            problems.append(f"SandboxAgent {name} has no {kind} condition")
        elif condition.get("status") != "True":
            problems.append(f"SandboxAgent {name} {kind}={condition.get('status')} ({condition.get('reason')})")
        elif generation is not None and condition.get("observedGeneration", generation) != generation:
            problems.append(f"SandboxAgent {name} {kind} is for generation {condition.get('observedGeneration')}, "
                            f"not {generation}")
    if generation is not None and status.get("observedGeneration") != generation:
        problems.append(f"SandboxAgent {name} observedGeneration {status.get('observedGeneration')} != {generation}")
    spec = item.get("spec") or {}
    if ((spec.get("byo") or {}).get("deployment") or {}).get("image") != image:
        problems.append(f"SandboxAgent {name} does not declare the release image")
    if (spec.get("substrate") or {}).get("workerPoolRef") != POOL_REF:
        problems.append(f"SandboxAgent {name} is not on WorkerPool {WORKER_POOL}")
    return problems


def assess_worker_pool(pool: Mapping[str, Any] | None) -> list[str]:
    """The shared WorkerPool is a prerequisite: it is verified, never created or scaled by CI/CD."""

    if not pool:
        return [f"WorkerPool {NAMESPACE}/{WORKER_POOL} is missing"]
    problems = []
    spec, status = pool.get("spec") or {}, pool.get("status") or {}
    if spec.get("sandboxClass") != SANDBOX_CLASS:
        problems.append(f"WorkerPool {WORKER_POOL} sandboxClass is {spec.get('sandboxClass')}, not {SANDBOX_CLASS}")
    replicas = status.get("replicas")
    if not isinstance(replicas, int) or replicas < 1:
        problems.append(f"WorkerPool {WORKER_POOL} has no workers (status.replicas={replicas})")
    elif isinstance(spec.get("replicas"), int) and replicas != spec["replicas"]:
        problems.append(f"WorkerPool {WORKER_POOL} is scaling ({replicas}/{spec['replicas']})")
    return problems


def assess_actor_template(templates: list[Mapping[str, Any]], agent: Mapping[str, Any], *, image: str,
                          module: str) -> tuple[list[str], str | None]:
    """The Substrate actor of one SandboxAgent; returns (problems, golden actor id)."""

    name = (agent.get("metadata") or {}).get("name")
    uid = (agent.get("metadata") or {}).get("uid")
    owned = [t for t in templates
             if any(o.get("kind") == "SandboxAgent" and o.get("uid") == uid and o.get("controller")
                    for o in (t.get("metadata") or {}).get("ownerReferences") or [])]
    if len(owned) != 1:
        return [f"SandboxAgent {name} controls {len(owned)} ActorTemplates (expected 1)"], None
    template = owned[0]
    spec, status = template.get("spec") or {}, template.get("status") or {}
    problems = []
    if spec.get("sandboxClass") != SANDBOX_CLASS:
        problems.append(f"ActorTemplate of {name} is not gVisor ({spec.get('sandboxClass')})")
    if (spec.get("workerSelector") or {}).get("matchLabels") != WORKER_SELECTOR:
        problems.append(f"ActorTemplate of {name} does not select WorkerPool {WORKER_POOL}")
    containers = spec.get("containers") or []
    if len(containers) != 1 or containers[0].get("image") != image:
        problems.append(f"ActorTemplate of {name} does not run the release image")
    elif module not in (containers[0].get("command") or []) + (containers[0].get("args") or []):
        problems.append(f"ActorTemplate of {name} does not start {module}")
    if conditions(template).get("Ready", {}).get("status") != "True":
        problems.append(f"ActorTemplate of {name} is not Ready (phase {status.get('phase')})")
    golden = status.get("goldenActorID")
    if not golden:
        problems.append(f"ActorTemplate of {name} has no golden actor (Substrate actor startup incomplete)")
    return problems, golden


# --- reconciliation --------------------------------------------------------------------------------------------
def reconcile(*, expected_sha: str, plan_sha: str, manifest: Mapping[str, Any], image_name: str,
              deployed: Mapping[str, str]) -> list[str]:
    """Expected SHA = artifact provenance = plan SHA = deployed identity.

    ``deployed`` maps an object (``SandboxAgent/<name>``) to the image digest reference it runs; a
    candidate also carries the release SHA label, checked by the caller through ``deployed_sha``.
    """

    problems = []
    if not SHA.fullmatch(expected_sha):
        problems.append("expected SHA is not a full SHA")
    if plan_sha != expected_sha:
        problems.append(f"plan SHA {plan_sha} is not the expected SHA {expected_sha}")
    if manifest.get("git_sha") != expected_sha:
        problems.append(f"image manifest was built for {manifest.get('git_sha')}, not {expected_sha}")
    entry = (manifest.get("images") or {}).get(image_name) or {}
    if entry.get("revision", manifest.get("git_sha")) != expected_sha:
        problems.append(f"image {image_name} provenance revision {entry.get('revision')} is not {expected_sha}")
    reference = entry.get("digest_ref")
    if not reference:
        problems.append(f"image manifest has no digest for {image_name}")
    for target, running in sorted(deployed.items()):
        if running != reference:
            problems.append(f"{target} runs {running}, not the released digest")
    return problems
