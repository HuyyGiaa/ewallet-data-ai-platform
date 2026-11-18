"""Release unit kind ``agent-staging``: deploy the real Agent stack into the staging namespaces, verify it, remove it.

V2-P3A (docs/workplans/edai2/CICD_V2_P3A_STAGING_PREPARATION.md). Unlike the bounded rehearsals (smoke Jobs in
ewallet-cicd) and the Option B+ candidate (production-namespaced), this deploys the real runtime in isolation:

1. preflight (read-only): the contract names only staging namespaces; the images are release digests built from
   the release SHA; the CRDs are served; both namespaces exist; every staging Secret and ConfigMap exists (Secrets
   by metadata only); nothing of an earlier run is left (no Helm release, no fleet object). The drift artifact
   pins (sha256 and dataset revision) are read from the staging ConfigMaps.
2. ``helm template`` the staging profile and refuse any object outside ewallet-agents-staging, then
   ``helm install llm-agent-staging`` (Coordinator Agent, A2A and MCP Gateways, both MCP servers, idempotency Redis).
3. create the WorkerPool, ServiceAccounts and SandboxAgents of infra/kubernetes/agent-staging/fleet.yaml.tmpl
   (create-only, labelled with this run's execution id) and wait until the Coordinator Deployment, the WorkerPool,
   the SandboxAgents and their golden actors are ready (sandbox_agents.assess_*).
4. verify: each Specialist directly through the kagent SandboxAgent route with the pinned known answers
   (specialist_e2e), the Coordinator fraud/drift/combined cases through the staging A2A Gateway (verdict by
   ops/llm_agent/replay_verdict.e2e), and the security negatives (anonymous MCP refused, undeclared and production
   A2A hosts unrouted).
5. cleanup, always: delete exactly the objects this run created (same uid, this execution's label), then
   ``helm uninstall`` the release this run installed. Anything not proven gone is reported, never retried blindly.

Nothing falls back to production: a missing staging dependency is a failure. NL intake stays off in the staging
profile, so a PASS records AGENT_E2E_VERIFIED and MANAGED_PROMPT_RUNTIME_NOT_YET_VERIFIED separately.
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.ci import sandbox_agents as sa
from scripts.ci import specialist_e2e
from scripts.ci.release_units import PRODUCTION_NAMESPACES

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "jenkins/cd/agent-staging.contract.json"
AGENTS_NAMESPACE = "ewallet-agents-staging"
SANDBOX_NAMESPACE = "ewallet-sandbox-staging"
RELEASE = "llm-agent-staging"
IMAGES = ("g3-byo-coordinator", "mcp-tools")
CRDS = (("kagent.dev/v1alpha2", "sandboxagents"), ("kagent.dev/v1alpha2", "agents"),
        ("ate.dev/v1alpha1", "workerpools"), ("ate.dev/v1alpha1", "actortemplates"))
Helm = Callable[[Sequence[str]], subprocess.CompletedProcess]
Forward = Callable[[Mapping[str, Any]], contextlib.AbstractContextManager[str]]
Status = Callable[[str, bytes, Mapping[str, str]], int | None]


class StagingContractError(ValueError):
    """The staging contract could write outside the staging namespaces."""


@dataclass
class StagingReport:
    unit: str
    execution_id: str
    status: str = "PENDING"
    failure_code: str | None = None
    problems: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    helm_installed: bool = False
    created: list[dict[str, str]] = field(default_factory=list)
    e2e: list[dict[str, Any]] = field(default_factory=list)
    negatives: list[dict[str, Any]] = field(default_factory=list)
    cleanup: list[dict[str, str]] = field(default_factory=list)
    left_behind: list[dict[str, str]] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)

    def fail(self, code: str, problems: list[str]) -> None:
        if self.failure_code is None:
            self.failure_code, self.status = code, "FAIL"
        self.problems.extend(problems)


def load_contract(path: Path = CONTRACT) -> dict[str, Any]:
    contract = json.loads(path.read_text(encoding="utf-8"))
    namespaces = (contract.get("agents_namespace"), contract.get("sandbox_namespace"))
    if namespaces != (AGENTS_NAMESPACE, SANDBOX_NAMESPACE) or set(namespaces) & PRODUCTION_NAMESPACES:
        raise StagingContractError(f"the staging contract must target {AGENTS_NAMESPACE} and {SANDBOX_NAMESPACE}")
    if contract.get("helm", {}).get("release") != RELEASE:
        raise StagingContractError(f"the staging Helm release must be {RELEASE}")
    if not contract.get("snapshot_prefix", "").startswith(f"gs://ate-snapshots/{SANDBOX_NAMESPACE}/"):
        raise StagingContractError("the staging snapshot prefix must be under ewallet-sandbox-staging")
    secrets = [s for scope in contract["required"].values() for s in scope["secrets"]]
    secrets += [agent["credential_secret"] for agent in contract["agents"].values()]
    if any(not ("-staging" in name) for name in secrets):
        raise StagingContractError("every staging Secret reference must be a dedicated -staging Secret")
    if set(contract["agents"]) != {"feature-rag-specialist", "drift-specialist"}:
        raise StagingContractError("the staging fleet is exactly the two Specialists")
    return contract


# --- rendering -----------------------------------------------------------------------------------------------
def helm_install_args(contract: Mapping[str, Any], images: Mapping[str, str], git_sha: str, execution_id: str,
                      drift: Mapping[str, str]) -> list[str]:
    """``--set`` values of the staging release: only release identities; everything else is values-staging.yaml."""

    helm = contract["helm"]
    mcp = images["mcp-tools"]
    sets = {
        "agents.coordinator.byo.image": images["g3-byo-coordinator"],
        "mcpServices.image": mcp,
        "mcpServices.identity.imageDigest": "sha256:" + mcp.rsplit("@sha256:", 1)[1],
        "mcpServices.identity.gitSha": git_sha,
        "mcpServices.identity.configRevision": f"staging-{execution_id}",
        "mcpServices.services.drift-mcp.env.EWALLET_DRIFT_BASELINES_SHA256": drift["baselines_sha256"],
        "mcpServices.services.drift-mcp.env.EWALLET_DRIFT_WINDOW_MANIFEST_SHA256": drift["window_sha256"],
        "mcpServices.services.drift-mcp.env.EWALLET_DRIFT_DATASET_REVISION": drift["dataset_revision"],
    }
    args = [RELEASE, str(ROOT / helm["chart"]), "--namespace", AGENTS_NAMESPACE,
            "-f", str(ROOT / helm["chart"] / helm["values"])]
    for key, value in sets.items():
        args += ["--set-string", f"{key}={value}"]
    return args


def unsafe_objects(manifest: str) -> list[str]:
    """Every rendered object outside the staging Agent namespace (the chart guard already refuses most)."""

    import yaml

    return [f"{d.get('kind')}/{(d.get('metadata') or {}).get('namespace')}/{(d.get('metadata') or {}).get('name')}"
            for d in yaml.safe_load_all(manifest) if d and (d.get("metadata") or {}).get("namespace") != AGENTS_NAMESPACE]


def render_fleet(contract: Mapping[str, Any], image: str, git_sha: str, execution_id: str) -> list[dict[str, Any]]:
    import yaml

    if not sa.DIGEST_REF.fullmatch(image) or not sa.SHA.fullmatch(git_sha) or not sa.EXECUTION_ID.fullmatch(execution_id):
        raise StagingContractError("image digest, release SHA and execution id are required")
    text = (ROOT / contract["fleet_template"]).read_text(encoding="utf-8")
    text = text.replace("__AGENT_IMAGE__", image).replace("__EXECUTION__", execution_id).replace("__RELEASE_SHA__", git_sha)
    if "__" in "".join(line for line in text.splitlines() if not line.lstrip().startswith("#")):
        raise StagingContractError("an unfilled placeholder is left in the fleet template")
    documents = [doc for doc in yaml.safe_load_all(text) if doc]
    for doc in documents:
        meta = doc["metadata"]
        if meta.get("namespace") != SANDBOX_NAMESPACE or meta.get("labels", {}).get(sa.EXECUTION_LABEL) != execution_id:
            raise StagingContractError(f"{doc['kind']}/{meta.get('name')} must be in {SANDBOX_NAMESPACE} with this run's label")
        if doc["kind"] == "SandboxAgent":
            if doc["spec"]["substrate"]["workerPoolRef"]["name"] != contract["worker_pool"]:
                raise StagingContractError("a staging SandboxAgent must use the staging WorkerPool")
            if not doc["spec"]["substrate"]["snapshotsConfig"]["location"].startswith(contract["snapshot_prefix"]):
                raise StagingContractError("a staging SandboxAgent must snapshot under the staging prefix")
    return documents


# --- steps -----------------------------------------------------------------------------------------------------
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def preflight(kube: Any, unit: Mapping[str, Any], plan: Mapping[str, Any], manifest: Mapping[str, Any],
              contract: Mapping[str, Any], helm: Helm, report: StagingReport) -> dict[str, str] | None:
    """Read-only. Returns the drift artifact pins, or None with ``report`` failed."""

    if plan.get("stage") != "rehearsal" or unit.get("target_namespace") != AGENTS_NAMESPACE:
        report.fail("WRONG_TARGET", [f"agent-staging runs only as an approved rehearsal into {AGENTS_NAMESPACE}"])
        return None
    images = unit.get("images") or {}
    identity = [] if set(images) == set(IMAGES) else [f"images must be exactly {list(IMAGES)}"]
    for name in IMAGES if not identity else ():
        identity += sa.reconcile(expected_sha=plan["git_sha"], plan_sha=plan["git_sha"], manifest=manifest,
                                 image_name=name, deployed={f"plan image {name}": images[name]})
    if identity:
        report.fail("IDENTITY_MISMATCH", identity)
        return None
    missing_crds = [f"{plural}.{gv}" for gv, plural in CRDS if not kube.available(gv, plural)]
    if missing_crds:
        report.fail("MISSING_CRD", missing_crds)
        return None
    missing = [f"namespace {ns}" for ns in (AGENTS_NAMESPACE, SANDBOX_NAMESPACE) if not kube.exists("ServiceAccount", ns, "default")]
    for namespace, required in contract["required"].items():
        missing += [f"Secret {namespace}/{n}" for n in required["secrets"] if not kube.exists("Secret", namespace, n)]
        missing += [f"ConfigMap {namespace}/{n}" for n in required["configmaps"] if not kube.exists("ConfigMap", namespace, n)]
    if missing:
        report.fail("MISSING_PREREQUISITE", missing)   # never replaced by a production dependency
        return None
    stale = [f"Helm release {AGENTS_NAMESPACE}/{RELEASE}"] if helm(["status", RELEASE, "--namespace", AGENTS_NAMESPACE]).returncode == 0 else []
    stale += [f"WorkerPool {SANDBOX_NAMESPACE}/{contract['worker_pool']}"] if kube.get("WorkerPool", SANDBOX_NAMESPACE, contract["worker_pool"]) else []
    stale += [f"SandboxAgent {SANDBOX_NAMESPACE}/{a}" for a in contract["agents"] if kube.get("SandboxAgent", SANDBOX_NAMESPACE, a)]
    if stale:
        report.fail("STALE_STAGING", [f"{item} already exists; it is never adopted (remove it by hand after inspection)"
                                      for item in stale])
        return None
    artifacts = contract["drift_artifacts"]
    baselines = (kube.get("ConfigMap", AGENTS_NAMESPACE, artifacts["baselines"]["configmap"]) or {}).get("data", {})
    window = (kube.get("ConfigMap", AGENTS_NAMESPACE, artifacts["window"]["configmap"]) or {}).get("data", {})
    try:
        window_text = window[artifacts["window"]["key"]]
        revision = json.loads(window_text)["lineage"]["snapshot_id"]
        return {"baselines_sha256": _sha(baselines[artifacts["baselines"]["key"]]), "window_sha256": _sha(window_text),
                "dataset_revision": str(revision)}
    except (KeyError, TypeError, ValueError):
        report.fail("MISSING_PREREQUISITE", ["the staging drift ConfigMaps lack baselines, window manifest or lineage.snapshot_id"])
        return None


def _wait_ready(kube: Any, contract: Mapping[str, Any], image: str, clock: Callable[[], float],
                sleep: Callable[[float], None], report: StagingReport) -> bool:
    pool = contract["worker_pool"]
    pool_ref = {"apiGroup": "ate.dev", "kind": "WorkerPool", "name": pool}
    deadline = clock() + contract["timeouts"]["ready_seconds"]
    while True:
        coordinator = kube.get("Deployment", AGENTS_NAMESPACE, contract["coordinator"]["name"]) or {}
        status = coordinator.get("status") or {}
        problems = [] if status.get("readyReplicas", 0) >= 1 else [f"Coordinator Deployment {AGENTS_NAMESPACE}/{contract['coordinator']['name']} not ready"]
        problems += sa.assess_worker_pool(kube.get("WorkerPool", SANDBOX_NAMESPACE, pool), namespace=SANDBOX_NAMESPACE, name=pool)
        templates = kube.list("ActorTemplate", SANDBOX_NAMESPACE)
        for agent, spec in contract["agents"].items():
            item = kube.get("SandboxAgent", SANDBOX_NAMESPACE, agent)
            problems += sa.assess_sandbox_agent(item, image=image, name=agent, pool_ref=pool_ref)
            problems += sa.assess_actor_template(templates, item or {}, image=image, module=spec["module"], worker_pool=pool)[0]
        if not problems:
            return True
        if clock() >= deadline:
            report.fail("NOT_READY", problems)
            return False
        sleep(10)


def http_status(url: str, body: bytes, headers: Mapping[str, str]) -> int | None:
    try:
        with urlopen(Request(url, data=body, method="POST", headers=dict(headers)), timeout=30) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except (URLError, OSError):
        return None


def _requests(contract: Mapping[str, Any], case: str, request_id: str) -> dict[str, Any]:
    """The Coordinator request of one case, built by agi_probe from the Specialists' own typed fixtures."""

    from ai_platform.llm_agent.business_request import a2a_message_send
    from scripts.llm_agent import agi_probe

    def fixture(agent: str) -> dict[str, Any]:
        text = (ROOT / contract["agents"][agent]["fixture"]).read_text(encoding="utf-8")
        part = json.loads(text)["params"]["message"]["parts"][0]["text"]
        return json.loads(part.split("REQUEST_JSON=", 1)[1].replace("__TASK_ID__", "fixture"))

    fraud, drift = fixture("feature-rag-specialist"), fixture("drift-specialist")
    args = SimpleNamespace(kind=case, request_id=request_id, transaction=None, policy_query=fraud["knowledge_query"],
                           feature=drift["feature_name"], baseline=drift["baseline_id"],
                           window_start=drift["current_window"]["start"], window_end=drift["current_window"]["end"])
    if case == "drift":
        typed = agi_probe.drift_request(request_id, args)
    elif case == "fraud":
        typed = agi_probe.fraud_request(request_id, fraud["transaction"], fraud["knowledge_query"])
    else:
        from ai_platform.llm_agent.business_request import G3WorkflowRequest, member_request_id

        typed = G3WorkflowRequest(request_id=request_id,
                                  fraud=agi_probe.fraud_request(member_request_id(request_id, "fraud"),
                                                                fraud["transaction"], fraud["knowledge_query"]),
                                  drift=agi_probe.drift_request(member_request_id(request_id, "drift"), args))
    return a2a_message_send(typed)


def verify(contract: Mapping[str, Any], plan: Mapping[str, Any], image: str, forward: Forward, post: specialist_e2e.Post,
           status: Status, evidence: Path, report: StagingReport, clock: Callable[[], float],
           sleep: Callable[[float], None]) -> None:
    from ops.llm_agent import replay_verdict
    from scripts.llm_agent import agi_probe

    view = {"namespace": SANDBOX_NAMESPACE, "agents": contract["agents"], "kagent": contract["kagent"],
            "timeouts": contract["timeouts"]}
    with forward({"namespace": contract["kagent"]["namespace"], "service": contract["kagent"]["service"],
                  "port": contract["kagent"]["port"]}) as endpoint:
        for agent in contract["agents"]:
            verdict = specialist_e2e.verify(contract=view, agent=agent, target=agent, endpoint=endpoint,
                                            environment=f"{plan['environment']}:{SANDBOX_NAMESPACE}", git_sha=plan["git_sha"],
                                            image=image, request_id=f"staging-{report.execution_id}-{agent}", post=post,
                                            clock=clock, sleep=sleep)
            report.e2e.append({"check": f"specialist-{agent}", **specialist_e2e.to_json(verdict)})
    gateway = contract["a2a_gateway"]
    with forward({"namespace": AGENTS_NAMESPACE, "service": gateway["service"], "port": gateway["port"]}) as endpoint:
        url = f"{endpoint.rstrip('/')}/api/a2a/{AGENTS_NAMESPACE}/{contract['coordinator']['name']}/"
        for case in contract["coordinator"]["cases"]:
            request_id = f"staging-{report.execution_id}-{case}"
            directory = evidence / "e2e"
            directory.mkdir(parents=True, exist_ok=True)
            try:
                reply, elapsed = specialist_e2e.run_task(post, url, _requests(contract, case, request_id), {},
                                                         timeout=contract["timeouts"]["e2e_seconds"], clock=clock, sleep=sleep)
            except (HTTPError, URLError, OSError, ValueError) as error:
                reply, elapsed = {"error": {"transport": type(error).__name__}}, 0.0
            (directory / f"{case}-task.json").write_text(json.dumps(reply, indent=1), encoding="utf-8")
            (directory / f"{case}-timing.json").write_text(json.dumps({"elapsed_seconds": round(elapsed, 3)}), encoding="utf-8")
            result, reasons, summary = replay_verdict.e2e(case, directory / f"{case}-task.json",
                                                          directory / f"{case}-timing.json", request_id)
            report.e2e.append({"check": f"coordinator-{case}", "verdict": "PASS" if result == "COMPLETE" else result,
                               "reasons": reasons, "summary": summary})
        # Negatives: an undeclared host and a production host are not routed by the staging A2A Gateway.
        request = evidence / "negative-a2a-request.json"
        request.write_text(json.dumps(_requests(contract, "drift", f"staging-{report.execution_id}-negative")), encoding="utf-8")
        control = f"/api/a2a-sandboxes/{SANDBOX_NAMESPACE}/drift-specialist/"
        for host in (f"payments-agent.{AGENTS_NAMESPACE}", f"{contract['coordinator']['name']}.ewallet-agents"):
            probe = agi_probe.a2a_route(SimpleNamespace(endpoint=endpoint, request=request, kagent_host=host, control_path=control,
                                                        expect="no-route", timeout=120.0))
            report.negatives.append({"check": f"a2a-unrouted {host}", "verdict": probe["verdict"], "reasons": probe["reasons"]})
    mcp = contract["mcp_gateway"]
    with forward({"namespace": AGENTS_NAMESPACE, "service": mcp["service"], "port": mcp["port"]}) as endpoint:
        initialize = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "staging-negative", "version": "1"}}}).encode()
        for route in ("/feature-rag/mcp", "/drift/mcp"):
            code = status(f"{endpoint.rstrip('/')}{route}", initialize,
                          {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
            report.negatives.append({"check": f"mcp-anonymous-refused {route}", "verdict": "PASS" if code in (401, 403) else "FAIL",
                                     "reasons": [] if code in (401, 403) else [f"anonymous call returned {code}, expected 401/403"]})
    failed = [f"{item['check']}: {item['verdict']} {item.get('reasons')}" for item in report.e2e + report.negatives
              if item["verdict"] != "PASS"]
    if failed:
        report.fail("E2E_FAILED", failed)


def cleanup(kube: Any, helm: Helm, report: StagingReport, *, clock: Callable[[], float], sleep: Callable[[float], None],
            timeout: float) -> None:
    """Remove exactly what this run created; anything not proven ours or not proven gone is reported."""

    owned = []
    for item in reversed(report.created):   # SandboxAgents first, the WorkerPool after them
        current = kube.get(item["kind"], item["namespace"], item["name"])
        if current is None:
            report.cleanup.append({**item, "state": "ALREADY_GONE"})
            continue
        meta = current.get("metadata") or {}
        if meta.get("uid") != item["uid"] or (meta.get("labels") or {}).get(sa.EXECUTION_LABEL) != report.execution_id:
            report.cleanup.append({**item, "state": "NOT_OURS_LEFT_UNTOUCHED"})
            report.left_behind.append(item)
            continue
        try:
            kube.delete_uid(item["kind"], item["namespace"], item["name"], item["uid"])
            owned.append(item)
            report.cleanup.append({**item, "state": "DELETE_REQUESTED"})
        except Exception as error:  # reported below; a delete failure never hides the others
            report.cleanup.append({**item, "state": f"DELETE_FAILED: {type(error).__name__}"})
            report.left_behind.append(item)
    deadline = clock() + timeout
    while owned:
        owned = [item for item in owned if (kube.get(item["kind"], item["namespace"], item["name"]) or {}).get(
            "metadata", {}).get("uid") == item["uid"]]
        if not owned or clock() >= deadline:
            break
        sleep(5)
    report.left_behind += owned
    if report.helm_installed:   # preflight proved no release existed, so this run owns it
        result = helm(["uninstall", RELEASE, "--namespace", AGENTS_NAMESPACE, "--wait", "--timeout", "5m"])
        report.cleanup.append({"kind": "HelmRelease", "namespace": AGENTS_NAMESPACE, "name": RELEASE,
                               "state": "UNINSTALLED" if result.returncode == 0 else "UNINSTALL_FAILED"})
        if result.returncode != 0:
            report.left_behind.append({"kind": "HelmRelease", "namespace": AGENTS_NAMESPACE, "name": RELEASE})
    if report.left_behind:
        report.fail("CLEANUP_INCOMPLETE", [f"left for manual inspection: {report.left_behind}"])
        report.status = "CLEANUP_INCOMPLETE"


def run(kube: Any, unit: Mapping[str, Any], plan: Mapping[str, Any], manifest: Mapping[str, Any], *, helm: Helm,
        forward: Forward, execution_id: str, contract: Mapping[str, Any] | None = None, evidence: Path | None = None,
        post: specialist_e2e.Post = specialist_e2e.http_post, status: Status = http_status,
        clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> StagingReport:
    contract = contract or load_contract()
    report = StagingReport(unit["name"], execution_id)
    evidence = (evidence or ROOT / "build" / "agent-staging") / execution_id
    evidence.mkdir(parents=True, exist_ok=True)
    drift = preflight(kube, unit, plan, manifest, contract, helm, report)
    report.steps.append("preflight")
    if drift is None:
        return _write(report, evidence)
    image = unit["images"]["g3-byo-coordinator"]
    args = helm_install_args(contract, unit["images"], plan["git_sha"], execution_id, drift)
    try:
        rendered = helm(["template", *args])
        report.steps.append("helm-template")
        unsafe = unsafe_objects(rendered.stdout) if rendered.returncode == 0 else [rendered.stderr[-400:]]
        if unsafe:
            report.fail("RENDER_UNSAFE", unsafe)
            return _write(report, evidence)
        documents = render_fleet(contract, image, plan["git_sha"], execution_id)
        installed = helm(["install", *args, "--wait", "--timeout", contract["helm"]["timeout"]])
        report.steps.append("helm-install")
        report.helm_installed = True   # set even on failure: a partial install is ours and is uninstalled
        if installed.returncode != 0:
            report.fail("HELM_FAILED", [installed.stderr[-400:]])
            return _write(report, evidence)
        for document in documents:
            response = kube.create(document)
            meta = document["metadata"]
            report.created.append({"kind": document["kind"], "namespace": meta["namespace"], "name": meta["name"],
                                   "uid": response["metadata"]["uid"]})
        report.steps.append("fleet-created")
        if _wait_ready(kube, contract, image, clock, sleep, report):
            report.steps.append("ready")
            verify(contract, plan, image, forward, post, status, evidence, report, clock, sleep)
            report.steps.append("verified")
    except Exception as error:  # an API refusal mid-run: cleanup below still runs
        report.fail("STAGING_ERROR", [f"{type(error).__name__}: {str(error)[:300]}"])
    finally:
        if report.helm_installed or report.created:
            cleanup(kube, helm, report, clock=clock, sleep=sleep, timeout=contract["timeouts"]["cleanup_seconds"])
            report.steps.append("cleanup")
    if report.failure_code is None:
        report.status = "PASS"
        report.verification = ["AGENT_E2E_VERIFIED", "MANAGED_PROMPT_RUNTIME_NOT_YET_VERIFIED"]
    return _write(report, evidence)


def _write(report: StagingReport, evidence: Path) -> StagingReport:
    (evidence / "staging-verdict.json").write_text(json.dumps(asdict(report), indent=2, sort_keys=True) + "\n",
                                                   encoding="utf-8")
    return report


# --- CLI (dispatched by scripts/ci/release_dispatch.py) ---------------------------------------------------------
def _helm_runner(context: str) -> Helm:
    selector = [] if context in ("", "in-cluster") else ["--kube-context", context]
    return lambda args: subprocess.run(["helm", *args, *selector], cwd=ROOT, capture_output=True, text=True, check=False)


def _forwarder(context: str) -> Forward:
    from scripts.ci.sandbox_candidate import kubectl_forward

    @contextlib.contextmanager
    def forward(target: Mapping[str, Any]) -> Iterator[str]:
        with kubectl_forward(context, {"kagent": target}) as endpoint:   # any Service, same owned port-forward
            yield endpoint
    return forward


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--unit", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--image", action="append", default=[], help="name=digest reference")
    parser.add_argument("--contract", type=Path, default=CONTRACT)
    parser.add_argument("--target-namespace", required=True)
    parser.add_argument("--image-manifest", type=Path, required=True)
    parser.add_argument("--environment", default="gke-cpu")
    parser.add_argument("--kube-context", default="in-cluster")
    args = parser.parse_args(argv)
    from scripts.ci.k8s_api import Client
    from scripts.ci.sandbox_candidate import ClientKube

    images = dict(item.split("=", 1) for item in args.image)
    unit = {"name": args.unit, "target_namespace": args.target_namespace, "images": images}
    plan = {"stage": "rehearsal", "git_sha": args.git_sha, "environment": args.environment}
    manifest_path = args.image_manifest if args.image_manifest.is_absolute() else ROOT / args.image_manifest
    report = run(ClientKube(Client.from_environment(None)), unit, plan, json.loads(manifest_path.read_text(encoding="utf-8")),
                 helm=_helm_runner(args.kube_context), forward=_forwarder(args.kube_context),
                 execution_id=sa.new_execution_id(), contract=load_contract(args.contract), evidence=args.evidence_dir)
    print(json.dumps({"status": report.status, "failure_code": report.failure_code, "steps": report.steps,
                      "verification": report.verification}, sort_keys=True))
    return 0 if report.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
