"""Release unit kind ``sandbox-candidate``: real candidate SandboxAgents on the shared WorkerPool, then deleted.

Option B+ (docs/edai2/cicd/CICD_REHEARSAL_RUNBOOK.md, §7): a fully isolated staging copy is not
possible without changing frozen code or production policy (hard-coded gateway hosts,
production-only sandbox routes, MCP authorization and credential tables). The candidate is therefore
a pair of additive, release-scoped SandboxAgents in ``ewallet-sandbox`` that reuse the existing
ServiceAccounts, MCP credential Secrets and the shared production WorkerPool (so this is NOT isolated
staging), exercised directly through the kagent SandboxAgent route. It runs only inside an approved
production stage, before any promotion, and it always removes what it created.

Every run has a fresh execution id (``sandbox_agents.new_execution_id``). It is the candidates'
``ewallet.io/cicd-execution`` label, part of the exact snapshot prefix
``cicd-<sha12>/<execution>/``, and of every A2A request id, so a retry of the same SHA never reuses an
earlier golden snapshot, idempotency entry or answer.

Steps (every refusal names a code; nothing is created before preflight passes):

1. preflight (read-only): production stage and target namespace (``WRONG_TARGET``); CRDs served
   (``MISSING_CRD``); the WorkerPool gVisor with workers (``MISSING_WORKER_POOL`` /
   ``WORKER_POOL_NOT_READY``); both MCP servers ready (``MISSING_MCP``); each production SandboxAgent,
   ServiceAccount and credential Secret present (``MISSING_PRODUCTION_FLEET`` /
   ``MISSING_CREDENTIALS``, Secret by metadata only); no leftover candidate (``STALE_CANDIDATE``, with
   its uid and execution for the recovery procedure); release identity (``IDENTITY_MISMATCH``);
2. create the candidates (create-only). A create whose response is lost is resolved by reading the
   object back: it is ours only if it carries this execution's label;
3. wait for Accepted/Ready at the current generation and a golden actor (``NOT_ACCEPTED``,
   ``NOT_READY``, ``ACTOR_STARTUP_FAILED``);
4. reconcile the deployed identity (label and digest) and run the typed Specialist E2E
   (``E2E_FAILED``) through a port-forward this process owns;
5. always delete what this run created, by uid and only after re-reading it (same uid, this
   execution's label), and wait until the candidates and their ActorTemplates are gone. Anything that
   cannot be proven gone is ``CLEANUP_INCOMPLETE`` with its exact kind, name and uid. Finally prove the
   production SandboxAgents did not change (``PRODUCTION_CHANGED`` / ``PRODUCTION_STATE_UNKNOWN``).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import selectors
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
    "candidate-cleanup": ("CLEANUP_INCOMPLETE", "PRODUCTION_CHANGED", "PRODUCTION_STATE_UNKNOWN"),
}
CRDS = (("kagent.dev/v1alpha2", "sandboxagents"), ("ate.dev/v1alpha1", "workerpools"),
        ("ate.dev/v1alpha1", "actortemplates"))
FORWARD_LINE = re.compile(r"Forwarding from 127\.0\.0\.1:([0-9]{2,5}) -> ([0-9]+)")
Event = Callable[[dict[str, Any]], None]


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
def kubectl_forward(context: str, contract: Mapping[str, Any], *, start_timeout: float = 30) -> Iterator[str]:
    """``kubectl port-forward`` to the kagent controller, owned by this process.

    The local port is chosen by kubectl (``:<remote>``) and read from this child's own
    ``Forwarding from 127.0.0.1:<port>`` line, so a pre-existing listener on some fixed port can never be
    mistaken for the controller. The child is terminated and reaped (killed if it ignores SIGTERM).
    """

    kagent = contract["kagent"]
    process = subprocess.Popen(["kubectl", "--context", context, "-n", kagent["namespace"], "port-forward",
                                "--address", "127.0.0.1", f"svc/{kagent['service']}", f":{kagent['port']}"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        port = _forwarded_port(process, int(kagent["port"]), start_timeout)
        with socket.socket() as probe:
            probe.settimeout(5)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                raise RuntimeError("the kagent controller port-forward announced a port that does not accept connections")
        yield f"http://127.0.0.1:{port}/"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _forwarded_port(process: subprocess.Popen, remote: int, timeout: float) -> int:
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if selector.select(timeout=max(0.0, deadline - time.monotonic())):
                line = process.stdout.readline()
                match = FORWARD_LINE.search(line or "")
                if match and int(match.group(2)) == remote:
                    return int(match.group(1))
    finally:
        selector.close()
    raise RuntimeError("the kagent controller port-forward did not start")


@dataclass
class CandidateReport:
    unit: str
    execution_id: str = ""
    status: str = "PENDING"
    failure_code: str | None = None
    problems: list[str] = field(default_factory=list)
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    snapshot_prefix: str = ""
    e2e: list[dict[str, Any]] = field(default_factory=list)
    cleanup: dict[str, str] = field(default_factory=dict)
    cleanup_incomplete: list[dict[str, str]] = field(default_factory=list)
    production_before: dict[str, dict[str, Any]] = field(default_factory=dict)
    production_unchanged: bool | None = None
    # Set when a checkpoint could not prove lock ownership or identity; the transaction re-raises it.
    ownership_error: Any = field(default=None, repr=False)

    def fail(self, code: str, problems: list[str], status: str = "FAIL") -> None:
        if self.failure_code is None:
            self.failure_code, self.status = code, status
        self.problems.extend(problems)


def _fingerprint(item: Mapping[str, Any] | None) -> dict[str, Any]:
    item = item or {}
    return {"uid": (item.get("metadata") or {}).get("uid"), "generation": (item.get("metadata") or {}).get("generation"),
            "image": (((item.get("spec") or {}).get("byo") or {}).get("deployment") or {}).get("image")}


def _labels(item: Mapping[str, Any] | None) -> dict[str, str]:
    return ((item or {}).get("metadata") or {}).get("labels") or {}


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
        leftover = kube.get("SandboxAgent", namespace, name)
        if leftover is not None:
            report.fail("STALE_CANDIDATE", [
                f"SandboxAgent {name} already exists (uid {_fingerprint(leftover)['uid']}, execution "
                f"{_labels(leftover).get(sa.EXECUTION_LABEL)}, release {_labels(leftover).get(sa.RELEASE_SHA_LABEL)}); "
                "it is never adopted or overwritten: recover it with production_release recovery-plan"], "BLOCKED")
            return None
        rendered[agent] = sa.render_candidate(contract, agent, image, plan["git_sha"], report.execution_id)
    return rendered


def _create(kube: Kube, document: dict, execution_id: str, created: dict[str, str], emit: Event) -> None:
    """Create-only; on a lost or failed response, adopt the object only if it carries this execution."""

    name = document["metadata"]["name"]
    emit({"event": "candidate-create-attempted", "name": name, "execution_id": execution_id})
    try:
        response = kube.create(document)
        created[name] = response["metadata"]["uid"]
    except Exception:
        current = kube.get("SandboxAgent", document["metadata"]["namespace"], name)
        if current is not None and _labels(current).get(sa.EXECUTION_LABEL) == execution_id:
            created[name] = _fingerprint(current)["uid"]  # the request succeeded; only its response was lost
            emit({"event": "candidate-created", "name": name, "uid": created[name], "recovered": True})
        raise
    emit({"event": "candidate-created", "name": name, "uid": created[name]})


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


def cleanup(kube: Kube, contract: Mapping[str, Any], created: Mapping[str, str], execution_id: str, *,
            clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
            emit: Event = lambda event: None) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Delete exactly the candidates this execution created; returns (per-name state, incomplete resources).

    Idempotent: an object already gone counts as deleted. An object with another uid or without this
    execution's label is not ours (recreated by someone else) and is never deleted. API errors are
    retried until the cleanup deadline; whatever cannot be proven gone is returned with its identity.
    """

    namespace = contract["namespace"]
    state: dict[str, str] = {}
    owned: dict[str, str] = {}
    for name, uid in created.items():
        try:
            current = kube.get("SandboxAgent", namespace, name)
        except Exception as error:
            current, state[name] = {"metadata": {"uid": uid}}, f"READ_FAILED: {type(error).__name__}"
        if current is None:
            state[name] = "DELETED"
            continue
        if _fingerprint(current)["uid"] != uid or (
                "labels" in (current.get("metadata") or {}) and _labels(current).get(sa.EXECUTION_LABEL) != execution_id):
            state[name] = "NOT_OURS_LEFT_UNTOUCHED"
            continue
        owned[name] = uid
        try:
            kube.delete_uid("SandboxAgent", namespace, name, uid)
            state[name] = "DELETE_REQUESTED"
            emit({"event": "candidate-delete-requested", "name": name, "uid": uid})
        except Exception as error:  # retried below by re-reading; never hides the other deletions
            state[name] = f"DELETE_FAILED: {type(error).__name__}"
    deadline = clock() + contract["timeouts"]["cleanup_seconds"]
    remaining: list[dict[str, str]] = []
    while owned:
        remaining = []
        try:
            for name, uid in owned.items():
                current = kube.get("SandboxAgent", namespace, name)
                if current is not None and _fingerprint(current)["uid"] == uid:
                    remaining.append({"kind": "SandboxAgent", "namespace": namespace, "name": name, "uid": uid})
                    if state[name].startswith("DELETE_FAILED"):
                        with contextlib.suppress(Exception):
                            kube.delete_uid("SandboxAgent", namespace, name, uid)
                            state[name] = "DELETE_REQUESTED"
            uids = set(owned.values())
            for template in kube.list("ActorTemplate", namespace):
                owners = (template.get("metadata") or {}).get("ownerReferences") or []
                owner = next((o for o in owners if o.get("uid") in uids), None)
                if owner:
                    remaining.append({"kind": "ActorTemplate", "namespace": namespace,
                                      "name": template["metadata"]["name"], "owner_uid": owner["uid"]})
        except Exception as error:  # an API timeout: unknown is not gone
            remaining = [{"kind": "SandboxAgent", "namespace": namespace, "name": n, "uid": u,
                          "unknown": type(error).__name__} for n, u in owned.items()]
        if not remaining:
            for name in owned:
                state[name] = "DELETED"
            emit({"event": "candidates-deleted", "names": sorted(owned)})
            return state, []
        if clock() >= deadline:
            return state, remaining
        sleep(5)
    return state, []


def run(kube: Kube, unit: Mapping[str, Any], plan: Mapping[str, Any], manifest: Mapping[str, Any], *,
        execute: bool, forward: Callable[[], contextlib.AbstractContextManager[str]],
        contract: Mapping[str, Any] | None = None, evidence: Path | None = None, execution_id: str | None = None,
        emit: Event = lambda event: None, checkpoint: Callable[[], None] = lambda: None,
        post: specialist_e2e.Post = specialist_e2e.http_post,
        clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> CandidateReport:
    contract = contract or sa.load_contract()
    report = CandidateReport(unit["name"], execution_id or sa.new_execution_id())
    if not sa.EXECUTION_ID.fullmatch(report.execution_id):
        report.fail("WRONG_TARGET", [f"invalid execution id {report.execution_id}"], "BLOCKED")
        return _write(report, evidence)
    report.snapshot_prefix = sa.snapshot_prefix(contract, plan["git_sha"], report.execution_id)
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
    from scripts.ci.release_lock import OwnershipUncertain

    created: dict[str, str] = {}
    try:
        checkpoint()  # ownership and identity before the first create
        for agent, document in rendered.items():
            _create(kube, document, report.execution_id, created, emit)
        if _wait_ready(kube, contract, rendered, image, clock, sleep, report):
            namespace = contract["namespace"]
            for agent, document in rendered.items():
                item = kube.get("SandboxAgent", namespace, document["metadata"]["name"]) or {}
                labels = _labels(item)
                if labels.get(sa.RELEASE_SHA_LABEL) != plan["git_sha"] or labels.get(sa.EXECUTION_LABEL) != report.execution_id:
                    report.fail("IDENTITY_MISMATCH", [f"{document['metadata']['name']} carries release "
                                                      f"{labels.get(sa.RELEASE_SHA_LABEL)} execution "
                                                      f"{labels.get(sa.EXECUTION_LABEL)}"])
                drift = sa.reconcile(expected_sha=plan["git_sha"], plan_sha=plan["git_sha"], manifest=manifest,
                                     image_name="g3-byo-coordinator",
                                     deployed={f"SandboxAgent/{document['metadata']['name']}":
                                               _fingerprint(item)["image"]})
                if drift:
                    report.fail("IDENTITY_MISMATCH", drift)
            if report.failure_code is None:
                checkpoint()  # after a readiness wait of up to 15 minutes
                with forward() as endpoint:
                    for agent, document in rendered.items():
                        verdict = specialist_e2e.verify(
                            contract=contract, agent=agent, target=document["metadata"]["name"], endpoint=endpoint,
                            environment=f"{plan['environment']}:{contract['namespace']}", git_sha=plan["git_sha"],
                            image=image, request_id=f"cicd-{report.execution_id}-{agent}", post=post,
                            clock=clock, sleep=sleep)
                        report.e2e.append(specialist_e2e.to_json(verdict))
                failed = [f"{item['target']}: {item['verdict']} ({item['gate']}) {item['reasons']}"
                          for item in report.e2e if item["verdict"] != "PASS"]
                if failed:
                    report.fail("E2E_FAILED", failed)
    except OwnershipUncertain as error:
        # Stop: no further candidate step. Cleanup below still runs (uid-guarded, this execution only).
        report.ownership_error = error
        report.fail(error.state, [str(error)])
    except Exception as error:  # API refusal mid-run: still clean up, report the cause
        report.fail("CANDIDATE_ERROR", [f"{type(error).__name__}: {str(error)[:300]}"])
    finally:
        if created:
            try:
                report.cleanup, report.cleanup_incomplete = cleanup(kube, contract, created, report.execution_id,
                                                                    clock=clock, sleep=sleep, emit=emit)
            except Exception as error:  # never let cleanup replace the primary failure
                report.cleanup_incomplete = [{"kind": "SandboxAgent", "namespace": contract["namespace"], "name": n,
                                              "uid": u, "unknown": type(error).__name__} for n, u in created.items()]
            if report.cleanup_incomplete:
                report.problems.append(f"cleanup incomplete: {report.cleanup_incomplete}")
                if report.failure_code is None:
                    report.failure_code = "CLEANUP_INCOMPLETE"
                report.status = "CLEANUP_INCOMPLETE"  # critical: dominates any earlier status
        try:
            after = {agent: _fingerprint(kube.get("SandboxAgent", contract["namespace"], agent))
                     for agent in report.production_before}
            report.production_unchanged = after == report.production_before
        except Exception as error:
            report.production_unchanged = None
            report.fail("PRODUCTION_STATE_UNKNOWN", [f"cannot re-read the production SandboxAgents: {type(error).__name__}"])
        if report.production_unchanged is False:
            report.fail("PRODUCTION_CHANGED", ["a production SandboxAgent changed while the candidate ran"])
    if report.failure_code is None:
        report.status = "PASS"
    return _write(report, evidence)


def _write(report: CandidateReport, evidence: Path | None) -> CandidateReport:
    if evidence is not None:
        directory = evidence / report.execution_id if report.execution_id else evidence
        directory.mkdir(parents=True, exist_ok=True)  # fresh evidence per execution, never overwritten
        data = {k: v for k, v in report.__dict__.items() if k != "ownership_error"}
        (directory / "candidate-verdict.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                                                          encoding="utf-8")
    return report
