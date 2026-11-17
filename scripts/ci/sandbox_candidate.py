"""Release unit kind ``sandbox-candidate``: real candidate SandboxAgents on the shared WorkerPool, then deleted.

Option B+ (docs/edai2/cicd/CICD_REHEARSAL_RUNBOOK.md, "Agent Platform release"): a fully isolated
staging copy is not possible without changing frozen code or production policy (hard-coded gateway
hosts, production-only sandbox routes, MCP authorization and credential tables). The candidate is
therefore a pair of additive, release-scoped SandboxAgents in ``ewallet-sandbox`` that reuse the
existing ServiceAccounts, MCP credential Secrets and WorkerPool, and are exercised directly through
the kagent SandboxAgent route. It runs only inside an approved production stage, before any
promotion, and it always removes what it created.

Steps (every refusal names a code; nothing is created before preflight passes):

1. preflight (read-only): production stage and target namespace; CRDs served (``MISSING_CRD``);
   the WorkerPool gVisor with workers (``MISSING_WORKER_POOL`` / ``WORKER_POOL_NOT_READY``); both MCP
   servers ready (``MISSING_MCP``); each production SandboxAgent, ServiceAccount and credential
   Secret present (``MISSING_PRODUCTION_FLEET`` / ``MISSING_CREDENTIALS``, Secret by metadata only);
   no leftover candidate (``STALE_CANDIDATE``); release identity reconciled (``IDENTITY_MISMATCH``);
2. create the candidates (create-only, never an overwrite);
3. wait for Accepted/Ready at the current generation and a golden actor (``NOT_ACCEPTED``,
   ``NOT_READY``, ``ACTOR_STARTUP_FAILED``);
4. reconcile the deployed identity (label and digest) and run the typed Specialist E2E
   (``E2E_FAILED``);
5. always delete the candidates by uid and wait until they and their ActorTemplates are gone
   (``CLEANUP_FAILED``), and prove the production SandboxAgents did not change
   (``PRODUCTION_CHANGED``).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import json
from pathlib import Path
import socket
import subprocess
import time
from typing import Any, Callable, Iterator, Mapping, Protocol

from scripts.ci import sandbox_agents as sa
from scripts.ci import specialist_e2e

MCP_NAMESPACE = "ewallet-mcp"
MCP_SERVERS = ("feature-rag-mcp", "drift-mcp")
# Every prerequisite/verify name a sandbox-candidate unit may declare, with the failure code that
# enforces it here (tests keep release-units.json and this table equal).
CHECKS = {
    "worker-pool-ready": ("MISSING_WORKER_POOL", "WORKER_POOL_NOT_READY"),
    "mcp-servers-ready": ("MISSING_MCP",),
    "sandbox-credentials-present": ("MISSING_CREDENTIALS",),
    "candidate-ready": ("NOT_ACCEPTED", "NOT_READY"),
    "candidate-gvisor-actor": ("ACTOR_STARTUP_FAILED",),
    "candidate-e2e-feature-rag": ("E2E_FAILED",),
    "candidate-e2e-drift": ("E2E_FAILED",),
    "candidate-cleanup": ("CLEANUP_FAILED", "PRODUCTION_CHANGED"),
}
CRDS = (("kagent.dev/v1alpha2", "sandboxagents"), ("ate.dev/v1alpha1", "workerpools"),
        ("ate.dev/v1alpha1", "actortemplates"))


class Kube(Protocol):
    def available(self, group_version: str, plural: str) -> bool: ...
    def get(self, kind: str, namespace: str, name: str) -> dict | None: ...
    def exists(self, kind: str, namespace: str, name: str) -> bool: ...
    def list(self, kind: str, namespace: str) -> list[dict]: ...
    def create(self, document: dict) -> dict: ...
    def delete_uid(self, kind: str, namespace: str, name: str, uid: str) -> None: ...
    def patch(self, kind: str, namespace: str, name: str, operations: list[dict]) -> dict: ...


class ClientKube:
    """``Kube`` over scripts/ci/k8s_api.py (production identity; RBAC in the inert proposal)."""

    def __init__(self, client: Any) -> None:
        from scripts.ci import k8s_api

        self.client, self.api = client, k8s_api

    def available(self, group_version: str, plural: str) -> bool:
        return self.api.api_available(self.client, group_version, plural)

    def get(self, kind: str, namespace: str, name: str) -> dict | None:
        try:
            return self.api.get(self.client, kind, namespace, name)
        except self.api.KubeError as error:
            if "HTTP 404" in str(error):
                return None
            raise

    def exists(self, kind: str, namespace: str, name: str) -> bool:
        return self.api.exists(self.client, kind, namespace, name)

    def list(self, kind: str, namespace: str) -> list[dict]:
        return self.api.list_objects(self.client, kind, namespace)

    def create(self, document: dict) -> dict:
        return self.api.create(self.client, document)

    def delete_uid(self, kind: str, namespace: str, name: str, uid: str) -> None:
        self.api.delete_uid(self.client, kind, namespace, name, uid)

    def patch(self, kind: str, namespace: str, name: str, operations: list[dict]) -> dict:
        return self.api.json_patch(self.client, kind, namespace, name, operations)


@contextlib.contextmanager
def kubectl_forward(context: str, contract: Mapping[str, Any], port: int = 18393) -> Iterator[str]:
    """``kubectl port-forward`` to the kagent controller under the explicit production context."""

    kagent = contract["kagent"]
    process = subprocess.Popen(["kubectl", "--context", context, "-n", kagent["namespace"], "port-forward",
                                f"svc/{kagent['service']}", f"{port}:{kagent['port']}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while True:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("kagent controller port-forward did not start")
            time.sleep(1)
        yield f"http://127.0.0.1:{port}/"
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)


@dataclass
class CandidateReport:
    unit: str
    status: str = "PENDING"
    failure_code: str | None = None
    problems: list[str] = field(default_factory=list)
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    e2e: list[dict[str, Any]] = field(default_factory=list)
    cleanup: dict[str, str] = field(default_factory=dict)
    production_before: dict[str, dict[str, Any]] = field(default_factory=dict)
    production_unchanged: bool | None = None

    def fail(self, code: str, problems: list[str], status: str = "FAIL") -> None:
        if self.failure_code is None:
            self.failure_code, self.status = code, status
        self.problems.extend(problems)


def _fingerprint(item: Mapping[str, Any] | None) -> dict[str, Any]:
    item = item or {}
    return {"uid": (item.get("metadata") or {}).get("uid"), "generation": (item.get("metadata") or {}).get("generation"),
            "image": (((item.get("spec") or {}).get("byo") or {}).get("deployment") or {}).get("image")}


def _deployment_ready(item: Mapping[str, Any] | None) -> bool:
    if not item:
        return False
    wanted = (item.get("spec") or {}).get("replicas", 1)
    status = item.get("status") or {}
    return (status.get("observedGeneration", 0) >= (item.get("metadata") or {}).get("generation", 0)
            and status.get("readyReplicas", 0) >= max(wanted, 1))


def preflight(kube: Kube, unit: Mapping[str, Any], plan: Mapping[str, Any], contract: Mapping[str, Any],
              manifest: Mapping[str, Any], report: CandidateReport) -> dict[str, dict] | None:
    """Read-only checks; returns the rendered candidates, or None with ``report`` failed."""

    namespace = contract["namespace"]
    if plan.get("stage") != "production" or unit.get("target_namespace") != namespace:
        report.fail("WRONG_TARGET", [f"a sandbox candidate runs only in an approved production stage in {namespace}"],
                    "BLOCKED")
        return None
    missing_crds = [f"{plural}.{gv}" for gv, plural in CRDS if not kube.available(gv, plural)]
    if missing_crds:
        report.fail("MISSING_CRD", [f"CRD not served: {item}" for item in missing_crds], "BLOCKED")
        return None
    pool = sa.assess_worker_pool(kube.get("WorkerPool", namespace, contract["worker_pool"]))
    if pool:
        report.fail("MISSING_WORKER_POOL" if "missing" in pool[0] else "WORKER_POOL_NOT_READY", pool, "BLOCKED")
        return None
    mcp = [f"MCP server {MCP_NAMESPACE}/{name} is not ready" for name in MCP_SERVERS
           if not _deployment_ready(kube.get("Deployment", MCP_NAMESPACE, name))]
    if mcp:
        report.fail("MISSING_MCP", mcp, "BLOCKED")
        return None
    image = unit["images"]["g3-byo-coordinator"]
    identity = sa.reconcile(expected_sha=plan["git_sha"], plan_sha=plan["git_sha"], manifest=manifest,
                            image_name="g3-byo-coordinator", deployed={"plan unit": image})
    if identity:
        report.fail("IDENTITY_MISMATCH", identity, "BLOCKED")
        return None
    rendered: dict[str, dict] = {}
    for agent in sa.production_agents(contract):
        spec = contract["agents"][agent]
        production = kube.get("SandboxAgent", namespace, agent)
        if production is None:
            report.fail("MISSING_PRODUCTION_FLEET", [f"production SandboxAgent {agent} is missing"], "BLOCKED")
            return None
        report.production_before[agent] = _fingerprint(production)
        if not kube.exists("ServiceAccount", namespace, spec["service_account"]):
            report.fail("MISSING_CREDENTIALS", [f"ServiceAccount {spec['service_account']} is missing"], "BLOCKED")
            return None
        secret = sa.credential_secret(contract, agent)
        if not kube.exists("Secret", namespace, secret):
            report.fail("MISSING_CREDENTIALS", [f"MCP credential Secret {secret} is missing"], "BLOCKED")
            return None
        name = sa.candidate_name(contract, agent)
        if kube.get("SandboxAgent", namespace, name) is not None:
            report.fail("STALE_CANDIDATE", [f"SandboxAgent {name} already exists (a previous run did not clean up); "
                                            "remove it with the runbook procedure first"], "BLOCKED")
            return None
        rendered[agent] = sa.render_candidate(contract, agent, image, plan["git_sha"])
    return rendered


def _wait_ready(kube: Kube, contract: Mapping[str, Any], created: Mapping[str, dict], image: str,
                clock: Callable[[], float], sleep: Callable[[float], None], report: CandidateReport) -> bool:
    namespace = contract["namespace"]
    deadline = clock() + contract["timeouts"]["ready_seconds"]
    while True:
        problems, startup = [], []
        for agent, document in created.items():
            name = document["metadata"]["name"]
            item = kube.get("SandboxAgent", namespace, name)
            accepted = sa.conditions(item or {}).get("Accepted", {})
            if accepted.get("status") == "False":
                report.fail("NOT_ACCEPTED", [f"SandboxAgent {name} rejected: {accepted.get('reason')} "
                                             f"{accepted.get('message', '')[:200]}"])
                return False
            agent_problems = sa.assess_sandbox_agent(item, image=image, name=name)
            template_problems, golden = sa.assess_actor_template(
                kube.list("ActorTemplate", namespace), item or {}, image=image,
                module=contract["agents"][agent]["module"])
            problems += agent_problems
            startup += template_problems
            report.candidates[agent].update({"golden_actor": golden, "uid": (item or {}).get("metadata", {}).get("uid")})
        if not problems and not startup:
            return True
        if clock() >= deadline:
            report.fail("NOT_READY" if problems else "ACTOR_STARTUP_FAILED", problems + startup)
            return False
        sleep(10)


def _cleanup(kube: Kube, contract: Mapping[str, Any], created: Mapping[str, str], clock: Callable[[], float],
             sleep: Callable[[float], None], report: CandidateReport) -> None:
    namespace = contract["namespace"]
    for name, uid in created.items():
        try:
            kube.delete_uid("SandboxAgent", namespace, name, uid)
            report.cleanup[name] = "DELETE_REQUESTED"
        except Exception as error:  # an API failure must not hide the other deletions
            report.cleanup[name] = f"DELETE_FAILED: {type(error).__name__}"
    deadline = clock() + contract["timeouts"]["cleanup_seconds"]
    while True:
        remaining = [name for name in created if kube.get("SandboxAgent", namespace, name) is not None]
        uids = set(created.values())
        templates = [t["metadata"]["name"] for t in kube.list("ActorTemplate", namespace)
                     if any(o.get("uid") in uids for o in (t.get("metadata") or {}).get("ownerReferences") or [])]
        if not remaining and not templates:
            for name in created:
                report.cleanup[name] = "DELETED"
            return
        if clock() >= deadline:
            report.fail("CLEANUP_FAILED", [f"still present after cleanup: {sorted(remaining + templates)}"],
                        "CLEANUP_FAILED")
            report.status = "CLEANUP_FAILED"
            return
        sleep(5)


def run(kube: Kube, unit: Mapping[str, Any], plan: Mapping[str, Any], manifest: Mapping[str, Any], *,
        execute: bool, forward: Callable[[], contextlib.AbstractContextManager[str]],
        contract: Mapping[str, Any] | None = None, evidence: Path | None = None,
        post: specialist_e2e.Post = specialist_e2e.http_post, clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep) -> CandidateReport:
    contract = contract or sa.load_contract()
    report = CandidateReport(unit["name"])
    rendered = preflight(kube, unit, plan, contract, manifest, report)
    if rendered is None:
        return _write(report, evidence)
    image = unit["images"]["g3-byo-coordinator"]
    report.candidates = {agent: {"name": doc["metadata"]["name"], "image": image,
                                 "snapshot": doc["spec"]["substrate"]["snapshotsConfig"]["location"]}
                         for agent, doc in rendered.items()}
    if not execute:
        report.status = "CHECKED_NOT_EXECUTED"
        return _write(report, evidence)
    created: dict[str, str] = {}
    try:
        for agent, document in rendered.items():
            response = kube.create(document)
            created[document["metadata"]["name"]] = response["metadata"]["uid"]
        if _wait_ready(kube, contract, rendered, image, clock, sleep, report):
            namespace = contract["namespace"]
            for agent, document in rendered.items():
                item = kube.get("SandboxAgent", namespace, document["metadata"]["name"]) or {}
                labels = (item.get("metadata") or {}).get("labels") or {}
                if labels.get(sa.RELEASE_SHA_LABEL) != plan["git_sha"]:
                    report.fail("IDENTITY_MISMATCH", [f"{document['metadata']['name']} carries release SHA "
                                                      f"{labels.get(sa.RELEASE_SHA_LABEL)}"])
                drift = sa.reconcile(expected_sha=plan["git_sha"], plan_sha=plan["git_sha"], manifest=manifest,
                                     image_name="g3-byo-coordinator",
                                     deployed={f"SandboxAgent/{document['metadata']['name']}":
                                               _fingerprint(item)["image"]})
                if drift:
                    report.fail("IDENTITY_MISMATCH", drift)
            if report.failure_code is None:
                with forward() as endpoint:
                    for agent, document in rendered.items():
                        verdict = specialist_e2e.verify(
                            contract=contract, agent=agent, target=document["metadata"]["name"], endpoint=endpoint,
                            environment=f"{plan['environment']}:{contract['namespace']}", git_sha=plan["git_sha"],
                            image=image, request_id=f"cicd-{plan['git_sha'][:12]}-{agent}", post=post,
                            clock=clock, sleep=sleep)
                        report.e2e.append(specialist_e2e.to_json(verdict))
                failed = [f"{item['target']}: {item['verdict']} {item['reasons']}" for item in report.e2e
                          if item["verdict"] != "PASS"]
                if failed:
                    report.fail("E2E_FAILED", failed)
    except Exception as error:  # API refusal mid-run: still clean up, report the cause
        report.fail("CANDIDATE_ERROR", [f"{type(error).__name__}: {str(error)[:300]}"])
    finally:
        if created:
            _cleanup(kube, contract, created, clock, sleep, report)
        after = {agent: _fingerprint(kube.get("SandboxAgent", contract["namespace"], agent))
                 for agent in report.production_before}
        report.production_unchanged = after == report.production_before
        if not report.production_unchanged:
            report.fail("PRODUCTION_CHANGED", ["a production SandboxAgent changed while the candidate ran"])
    if report.failure_code is None:
        report.status = "PASS"
    return _write(report, evidence)


def _write(report: CandidateReport, evidence: Path | None) -> CandidateReport:
    if evidence is not None:
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "candidate-verdict.json").write_text(json.dumps(report.__dict__, indent=2, sort_keys=True) + "\n",
                                                         encoding="utf-8")
    return report
