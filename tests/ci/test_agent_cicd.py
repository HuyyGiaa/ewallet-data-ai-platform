"""Agent Platform CI/CD (Batch 2A): candidate SandboxAgents, fleet promotion, production transaction.

Everything runs against fakes: ``FakeKube`` behaves like the kagent/Substrate controllers for the
objects CI/CD touches (SandboxAgent conditions, the controlled ActorTemplate and its golden actor,
uid preconditions, JSON-patch test operations), and ``tests.ci.test_promote.FakeCluster`` stands in
for Helm. No cluster, registry or Jenkins is contacted.
"""

from __future__ import annotations

import contextlib
import copy
import json
from pathlib import Path
import re

import pytest
import yaml

from scripts.ci import fleet_promote, production_release, runtime_verify, sandbox_agents as sa, sandbox_candidate
from scripts.ci import specialist_e2e
from scripts.ci.k8s_api import KubeError
from tests.ci.test_promote import FakeCluster

ROOT = Path(__file__).resolve().parents[2]
SHA = "c" * 40
REG = "asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform"
OLD = f"{REG}/g3-byo-feature-rag-specialist@sha256:" + "a" * 64
NEW = f"{REG}/g3-byo-coordinator@sha256:" + "b" * 64
MCP_NEW = f"{REG}/mcp-tools@sha256:" + "b" * 64
CONTRACT = sa.load_contract()
EXEC = "20261118t101500-abc123"
ANSWERS = {"feature-rag-specialist": {"fraud_probability": 0.0639895560144887},
           "drift-specialist": {"psi": 0.0004795543627041439}}


def manifest(sha: str = SHA, digest: str = NEW) -> dict:
    return {"git_sha": sha, "images": {"g3-byo-coordinator": {"digest_ref": digest, "revision": sha},
                                       "mcp-tools": {"digest_ref": MCP_NEW, "revision": sha}}}


def unit(kind: str = "sandbox-candidate", **changes) -> dict:  # type: ignore[no-untyped-def]
    data = {"name": "agent-fleet-candidate" if kind == "sandbox-candidate" else "agent-fleet-production",
            "kind": kind, "stage": "production", "target_namespace": "ewallet-sandbox",
            "images": {"g3-byo-coordinator": NEW}}
    data.update(changes)
    return data


def production_plan(*units: dict, sha: str = SHA) -> dict:
    return {"stage": "production", "git_sha": sha, "environment": "gke-cpu", "units": list(units)}


# --- a fake kagent + Substrate control plane ------------------------------------------------------------------
class FakeKube:
    def __init__(self, *, crds: bool = True, pool: dict | None | str = "default", mcp_ready: bool = True,
                 secrets: bool = True, accounts: bool = True, fleet: bool = True, behavior: str = "ready",
                 delete_fails: bool = False, delete_sticks: bool = False, touch_production: bool = False,
                 create_fails_for: str | None = None, create_lost_for: str | None = None,
                 patch_fails_for: str | None = None, patch_lost_for: str | None = None,
                 list_errors: int = 0, rollback_never_ready: bool = False) -> None:
        self.crds, self.mcp_ready, self.behavior = crds, mcp_ready, behavior
        self.create_fails_for, self.create_lost_for = create_fails_for, create_lost_for
        self.patch_fails_for, self.patch_lost_for = patch_fails_for, patch_lost_for
        self.list_errors, self.rollback_never_ready = list_errors, rollback_never_ready
        self.delete_fails, self.delete_sticks, self.touch_production = delete_fails, delete_sticks, touch_production
        self.objects: dict[tuple[str, str, str], dict] = {}
        self.calls: list[tuple] = []
        self.uid = 0
        if pool == "default":
            pool = {"metadata": {"name": "business-agents"}, "spec": {"sandboxClass": "gvisor", "replicas": 2},
                    "status": {"replicas": 2}}
        if pool:
            self.objects[("WorkerPool", "ewallet-sandbox", "business-agents")] = pool
        for name in ("feature-rag-mcp", "drift-mcp"):
            self.objects[("Deployment", "ewallet-mcp", name)] = {
                "metadata": {"name": name, "generation": 3},
                "spec": {"replicas": 2},
                "status": {"observedGeneration": 3, "readyReplicas": 2 if mcp_ready else 0}}
        for agent, spec in CONTRACT["agents"].items():
            if accounts:
                self.objects[("ServiceAccount", "ewallet-sandbox", spec["service_account"])] = {"metadata": {}}
            if secrets:
                self.objects[("Secret", "ewallet-sandbox", sa.credential_secret(CONTRACT, agent))] = {"metadata": {}}
            if fleet:
                document = sa.frozen_documents(OLD, CONTRACT["credential_release"])[agent]
                self._admit(document, ready=True)

    # helpers
    def _admit(self, document: dict, *, ready: bool, generation: int = 1) -> dict:
        self.uid += 1
        item = copy.deepcopy(document)
        item["metadata"].update(uid=f"uid-{self.uid}", generation=generation)
        self._status(item, ready)
        self.objects[("SandboxAgent", "ewallet-sandbox", item["metadata"]["name"])] = item
        self._template(item, ready)
        return item

    def _status(self, item: dict, ready: bool, accepted: str = "True") -> None:
        generation = item["metadata"]["generation"]
        item["status"] = {"observedGeneration": generation, "conditions": [
            {"type": "Accepted", "status": accepted, "observedGeneration": generation, "reason": "Reconciled"},
            {"type": "Ready", "status": "True" if ready else "False", "observedGeneration": generation,
             "reason": "WorkloadReady" if ready else "Pending"}]}

    def _template(self, item: dict, golden: bool) -> None:
        name = item["metadata"]["name"]
        module = item["spec"]["byo"]["deployment"]["args"][-1]
        for key in [k for k, v in self.objects.items() if k[0] == "ActorTemplate"
                    and v["metadata"]["ownerReferences"][0]["uid"] == item["metadata"]["uid"]]:
            del self.objects[key]
        self.objects[("ActorTemplate", "ewallet-sandbox", f"{name}-0123456789abcdef")] = {
            "metadata": {"name": f"{name}-0123456789abcdef", "ownerReferences": [
                {"kind": "SandboxAgent", "name": name, "uid": item["metadata"]["uid"], "controller": True}]},
            "spec": {"sandboxClass": "gvisor", "workerSelector": {"matchLabels": {"kagent.dev/worker-pool": "business-agents"}},
                     "containers": [{"name": "kagent", "image": item["spec"]["byo"]["deployment"]["image"],
                                     "command": ["/opt/venv/bin/python", "-m", module]}]},
            "status": {"conditions": [{"type": "Ready", "status": "True" if golden else "False"}],
                       "goldenActorID": "golden-1" if golden else None, "phase": "Ready" if golden else "Pending"}}

    # Kube protocol
    def available(self, group_version: str, plural: str) -> bool:
        return self.crds

    def get(self, kind: str, namespace: str, name: str) -> dict | None:
        item = self.objects.get((kind, namespace, name))
        if self.touch_production and kind == "SandboxAgent" and name == "drift-specialist" and len(self.calls) > 2:
            item["metadata"]["generation"] += 1
            self.touch_production = False
        return copy.deepcopy(item) if item is not None else None

    def exists(self, kind: str, namespace: str, name: str) -> bool:
        return (kind, namespace, name) in self.objects

    def list(self, kind: str, namespace: str) -> list[dict]:
        if self.list_errors and any(c[0] == "delete" for c in self.calls):
            self.list_errors -= 1
            raise KubeError("GET -> timed out")  # an API timeout during cleanup
        return [copy.deepcopy(v) for (k, n, _), v in self.objects.items() if k == kind and n == namespace]

    def create(self, document: dict) -> dict:
        self.calls.append(("create", document["metadata"]["name"]))
        key = ("SandboxAgent", document["metadata"]["namespace"], document["metadata"]["name"])
        if key in self.objects:
            raise KubeError("POST -> HTTP 409: AlreadyExists")
        if document["metadata"]["name"] == self.create_fails_for:
            raise KubeError("POST -> HTTP 500: internal error")
        item = self._admit(document, ready=self.behavior == "ready")
        if document["metadata"]["name"] == self.create_lost_for:
            raise KubeError("POST -> connection reset")  # the object exists; the response was lost
        if self.behavior == "rejected":
            self._status(self.objects[key], False, accepted="False")
        if self.behavior == "actor-fails":
            self._status(self.objects[key], True)
            self._template(self.objects[key], golden=False)
        return copy.deepcopy(item)

    def delete_uid(self, kind: str, namespace: str, name: str, uid: str) -> None:
        self.calls.append(("delete", name, uid))
        if self.delete_fails:
            raise KubeError("DELETE -> HTTP 403: forbidden")
        item = self.objects.get((kind, namespace, name))
        if item is None or item["metadata"]["uid"] != uid:
            raise KubeError("DELETE -> HTTP 409: precondition failed")
        if self.delete_sticks:
            return
        del self.objects[(kind, namespace, name)]
        for key in [k for k, v in self.objects.items() if k[0] == "ActorTemplate"
                    and v["metadata"]["ownerReferences"][0]["uid"] == uid]:
            del self.objects[key]

    def patch(self, kind: str, namespace: str, name: str, operations: list[dict]) -> dict:
        self.calls.append(("patch", name, json.dumps(operations)))
        if name == self.patch_fails_for and operations[-1]["value"] == NEW:
            raise KubeError("PATCH -> HTTP 500: internal error")
        item = self.objects[(kind, namespace, name)]
        for op in operations:
            target = {"/metadata/uid": item["metadata"]["uid"],
                      fleet_promote.IMAGE_PATH: item["spec"]["byo"]["deployment"]["image"]}[op["path"]]
            if op["op"] == "test" and target != op["value"]:
                raise KubeError("PATCH -> HTTP 422: test operation failed")
        image = operations[-1]["value"]
        item["spec"]["byo"]["deployment"]["image"] = image
        item["metadata"]["generation"] += 1
        ready = not (self.behavior == "patch-not-ready" and image == NEW) and not (
            self.rollback_never_ready and image != NEW)
        self._status(item, ready)
        self._template(item, ready)
        if name == self.patch_lost_for and image == NEW:
            raise KubeError("PATCH -> connection reset")  # applied, response lost
        return copy.deepcopy(item)


def specialist_reply(agent: str, request_id: str, **changes) -> dict:  # type: ignore[no-untyped-def]
    evidence = {"status": "ok", "request_id": request_id, "contract_version": "g3-orchestration/v1",
                "prompt_revision": f"{agent}/byo-v1", "unavailable": [],
                "features": {"account_behavior__account_tx_count_1h": 3} if agent == "feature-rag-specialist" else {},
                "citations": [{"chunk_id": "policy-payment-anomaly:0000"}] if agent == "feature-rag-specialist" else [],
                "fraud_score": {"fraud_probability": 0.0639895560144887} if agent == "feature-rag-specialist" else None,
                "drift": [{"psi": 0.0004795543627041439}] if agent == "drift-specialist" else []}
    evidence.update(changes)
    evidence = {k: v for k, v in evidence.items() if v is not None}
    return {"jsonrpc": "2.0", "id": request_id, "result": {
        "kind": "task", "id": "t-1", "status": {"state": "completed"},
        "artifacts": [{"parts": [{"kind": "text", "text": json.dumps(evidence)}]}]}}


def fake_post(overrides: dict | None = None, seen: list | None = None):  # type: ignore[no-untyped-def]
    def post(url, body, headers, timeout):  # type: ignore[no-untyped-def]
        agent = re.search(r"/api/a2a-sandboxes/ewallet-sandbox/([a-z-]+?)(?:-cicd)?/$", url).group(1)
        if seen is not None:
            seen.append((url, headers.get("X-User-Id"), body["id"]))
        if overrides and agent in overrides:
            return overrides[agent](body["id"])
        return specialist_reply(agent, body["id"])
    return post


@contextlib.contextmanager
def loopback():  # type: ignore[no-untyped-def]
    yield "http://127.0.0.1:18393/"


def run_candidate(kube: FakeKube, *, execute: bool = True, post=None, plan: dict | None = None,  # type: ignore[no-untyped-def]
                  candidate: dict | None = None, data: dict | None = None):
    ticks = iter(range(0, 100000, 7))
    return sandbox_candidate.run(kube, candidate or unit(), plan or production_plan(unit()), data or manifest(),
                                 execute=execute, forward=loopback, contract=CONTRACT, post=post or fake_post(),
                                 execution_id=EXEC, clock=lambda: next(ticks), sleep=lambda s: None)


# --- rendering: the frozen deployment contract -----------------------------------------------------------------
def test_candidate_differs_from_the_frozen_fleet_only_in_name_labels_image_and_snapshot_prefix() -> None:
    for agent in sa.production_agents(CONTRACT):
        candidate = sa.render_candidate(CONTRACT, agent, NEW, SHA, EXEC)
        frozen = sa.frozen_documents(NEW, CONTRACT["credential_release"])[agent]
        assert candidate["metadata"]["name"] == f"{agent}-cicd"
        assert candidate["metadata"]["namespace"] == "ewallet-sandbox"
        assert candidate["metadata"]["labels"][sa.RELEASE_SHA_LABEL] == SHA
        assert candidate["metadata"]["labels"][sa.EXECUTION_LABEL] == EXEC
        assert candidate["spec"]["substrate"]["snapshotsConfig"]["location"] == \
            f"gs://ate-snapshots/ewallet-sandbox/cicd-{SHA[:12]}/{EXEC}/{agent}-cicd/"
        # Security posture and network restrictions are the frozen ones, untouched.
        deployment_spec = candidate["spec"]["byo"]["deployment"]
        assert deployment_spec["podSecurityContext"] == {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
        assert deployment_spec["securityContext"] == {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
        assert candidate["spec"]["sandbox"] == frozen["spec"]["sandbox"]
        assert candidate["spec"]["substrate"]["workerPoolRef"] == sa.POOL_REF
        deployment = candidate["spec"]["byo"]["deployment"]
        assert deployment["image"] == NEW and deployment["cmd"] == "/opt/venv/bin/python"
        assert deployment["args"] == ["-m", CONTRACT["agents"][agent]["module"]]
        assert deployment["serviceAccountName"] == CONTRACT["agents"][agent]["service_account"]
        secret = [e for e in deployment["env"] if e["name"] == "EWALLET_MCP_SERVICE_TOKEN"][0]
        assert secret["valueFrom"]["secretKeyRef"]["name"] == sa.credential_secret(CONTRACT, agent)
        assert sa.contract_differences(candidate, frozen) == []
        assert "agi-00000000T000000Z" not in json.dumps(candidate)
        assert "ewallet.io/campaign" not in candidate["metadata"]["labels"]  # never mistaken for the fleet


def test_contract_drift_and_unsafe_inputs_are_refused() -> None:
    frozen = sa.frozen_documents(NEW, CONTRACT["credential_release"])["drift-specialist"]
    candidate = sa.render_candidate(CONTRACT, "drift-specialist", NEW, SHA, EXEC)
    privileged = copy.deepcopy(candidate)
    privileged["spec"]["byo"]["deployment"]["securityContext"]["allowPrivilegeEscalation"] = True
    assert any("differs from the frozen fleet" in p for p in sa.contract_differences(privileged, frozen))
    wider = copy.deepcopy(candidate)
    wider["spec"]["sandbox"]["network"]["allowedDomains"].append("example.com")
    assert any("differs from the frozen fleet" in p for p in sa.contract_differences(wider, frozen))
    labelled = copy.deepcopy(candidate)
    labelled["metadata"]["labels"]["ewallet.io/campaign"] = "agi-x"
    assert any("labels must be exactly" in p for p in sa.contract_differences(labelled, frozen))
    tagged = copy.deepcopy(candidate)
    tagged["spec"]["byo"]["deployment"]["image"] = f"{REG}/g3:latest"
    assert any("immutable digest" in p for p in sa.contract_differences(tagged, frozen))
    shared = copy.deepcopy(candidate)
    shared["spec"]["substrate"]["snapshotsConfig"]["location"] = f"gs://ate-snapshots/ewallet-sandbox/cicd-{SHA[:12]}/drift-specialist-cicd/"
    assert any("cicd-<sha12>/<execution>" in p for p in sa.contract_differences(shared, frozen))
    tampered = copy.deepcopy(candidate)
    tampered["spec"]["byo"]["deployment"]["serviceAccountName"] = "default"
    assert any("differs from the frozen fleet" in p for p in sa.contract_differences(tampered, frozen))
    renamed = copy.deepcopy(candidate)
    renamed["metadata"]["name"] = "drift-specialist"
    assert any("production SandboxAgent name" in p for p in sa.contract_differences(renamed, frozen))
    moved = copy.deepcopy(candidate)
    moved["metadata"]["namespace"] = "ewallet-agents"
    assert any("must live in ewallet-sandbox" in p for p in sa.contract_differences(moved, frozen))
    campaign = copy.deepcopy(candidate)
    campaign["spec"]["substrate"]["snapshotsConfig"]["location"] = "gs://ate-snapshots/ewallet-sandbox/agi-x/drift-specialist/"
    assert any("cicd-<sha12>" in p for p in sa.contract_differences(campaign, frozen))
    for args, message in (((CONTRACT, "fraud-coordinator", NEW, SHA, EXEC), "not a production Specialist"),
                          ((CONTRACT, "drift-specialist", f"{REG}/g3:latest", SHA, EXEC), "immutable digest"),
                          ((CONTRACT, "drift-specialist", NEW, "abc", EXEC), "full lowercase SHA"),
                          ((CONTRACT, "drift-specialist", NEW, SHA, "run-1"), "execution id")):
        with pytest.raises(sa.SandboxContractError, match=message):
            sa.render_candidate(*args)


def test_the_contract_file_itself_is_checked(tmp_path) -> None:
    data = json.loads(sa.CONTRACT.read_text())
    for change, message in ((dict(namespace="ewallet-agents"), "ewallet-sandbox/business-agents"),
                            (dict(sandbox_class="runc"), "gVisor"),
                            (dict(agents={"drift-specialist": data["agents"]["drift-specialist"]}), "exactly the two")):
        path = tmp_path / "contract.json"
        path.write_text(json.dumps({**data, **change}))
        with pytest.raises(sa.SandboxContractError, match=message):
            sa.load_contract(path)


# --- readiness adapters -----------------------------------------------------------------------------------------
def ready_agent(**changes) -> dict:  # type: ignore[no-untyped-def]
    kube = FakeKube()
    item = kube.get("SandboxAgent", "ewallet-sandbox", "drift-specialist")
    item.update(changes)
    return item


def test_sandbox_agent_readiness_needs_accepted_ready_generation_image_and_pool() -> None:
    assert sa.assess_sandbox_agent(ready_agent(), image=OLD, name="d") == []
    stale = ready_agent()
    stale["metadata"]["generation"] = 2
    assert any("observedGeneration" in p for p in sa.assess_sandbox_agent(stale, image=OLD, name="d"))
    not_ready = ready_agent()
    not_ready["status"]["conditions"][1]["status"] = "False"
    assert any("Ready=False" in p for p in sa.assess_sandbox_agent(not_ready, image=OLD, name="d"))
    no_accepted = ready_agent()
    no_accepted["status"]["conditions"] = no_accepted["status"]["conditions"][1:]
    assert any("no Accepted" in p for p in sa.assess_sandbox_agent(no_accepted, image=OLD, name="d"))
    assert any("release image" in p for p in sa.assess_sandbox_agent(ready_agent(), image=NEW, name="d"))
    other_pool = ready_agent()
    other_pool["spec"]["substrate"]["workerPoolRef"]["name"] = "private-pool"
    assert any("not on WorkerPool" in p for p in sa.assess_sandbox_agent(other_pool, image=OLD, name="d"))
    assert sa.assess_sandbox_agent(None, image=OLD, name="d") == ["SandboxAgent d is missing"]


def test_worker_pool_is_a_verified_prerequisite() -> None:
    good = {"spec": {"sandboxClass": "gvisor", "replicas": 2}, "status": {"replicas": 2}}
    assert sa.assess_worker_pool(good) == []
    assert "missing" in sa.assess_worker_pool(None)[0]
    assert "not gvisor" in sa.assess_worker_pool({**good, "spec": {"sandboxClass": "runc"}})[0]
    assert "no workers" in sa.assess_worker_pool({**good, "status": {"replicas": 0}})[0]
    assert "scaling" in sa.assess_worker_pool({**good, "status": {"replicas": 1}})[0]


def test_actor_template_adapter_reads_the_real_shape() -> None:
    """The fake mirrors docs-recorded ActorTemplates (AGS-01): owner uid, gVisor, selector, golden actor."""

    kube = FakeKube()
    agent = kube.get("SandboxAgent", "ewallet-sandbox", "drift-specialist")
    templates = kube.list("ActorTemplate", "ewallet-sandbox")
    problems, golden = sa.assess_actor_template(templates, agent, image=OLD, module="ai_platform.llm_agent.byo_drift_specialist")
    assert (problems, golden) == ([], "golden-1")
    problems, _ = sa.assess_actor_template(templates, agent, image=OLD, module="ai_platform.llm_agent.byo_coordinator")
    assert any("does not start" in p for p in problems)
    runc = [copy.deepcopy(t) for t in templates]
    for t in runc:
        t["spec"]["sandboxClass"] = "runc"
        t["status"]["goldenActorID"] = None
    problems, _ = sa.assess_actor_template(runc, agent, image=OLD, module="ai_platform.llm_agent.byo_drift_specialist")
    assert any("not gVisor" in p for p in problems) and any("golden actor" in p for p in problems)
    problems, _ = sa.assess_actor_template([], agent, image=OLD, module="x")
    assert problems == ["SandboxAgent drift-specialist controls 0 ActorTemplates (expected 1)"]


def test_identity_reconciliation_names_every_mismatch() -> None:
    assert sa.reconcile(expected_sha=SHA, plan_sha=SHA, manifest=manifest(), image_name="g3-byo-coordinator",
                        deployed={"SandboxAgent/x": NEW}) == []
    problems = sa.reconcile(expected_sha=SHA, plan_sha="d" * 40, manifest=manifest(sha="e" * 40),
                            image_name="g3-byo-coordinator", deployed={"SandboxAgent/x": OLD})
    assert any("plan SHA" in p for p in problems)
    assert any("image manifest was built for" in p for p in problems)
    assert any("runs" in p and "not the released digest" in p for p in problems)


# --- direct Specialist E2E -----------------------------------------------------------------------------------
@pytest.mark.parametrize("endpoint", ["http://kagent-controller.kagent:8083/", "https://127.0.0.1:18393/",
                                      "http://127.0.0.1:80/", "http://127.0.0.1:18393/@evil/", "http://localhost:18393/"])
def test_only_a_loopback_port_forward_is_an_allowed_endpoint(endpoint) -> None:
    with pytest.raises(specialist_e2e.EndpointError):
        specialist_e2e.sandbox_url(endpoint, "ewallet-sandbox", "drift-specialist-cicd")


def verify_with(reply, agent: str = "feature-rag-specialist"):  # type: ignore[no-untyped-def]
    return specialist_e2e.verify(contract=CONTRACT, agent=agent, target=f"{agent}-cicd", endpoint="http://127.0.0.1:18393/",
                                 environment="gke-cpu:ewallet-sandbox", git_sha=SHA, image=NEW, request_id="rid-1",
                                 post=lambda url, body, headers, timeout: reply(body["id"]),
                                 clock=iter(range(0, 10000, 30)).__next__, sleep=lambda s: None)


def test_known_answers_pass_for_both_specialists_with_the_real_fixtures() -> None:
    seen: list = []
    for agent in sa.production_agents(CONTRACT):
        result = specialist_e2e.verify(contract=CONTRACT, agent=agent, target=f"{agent}-cicd",
                                       endpoint="http://127.0.0.1:18393/", environment="gke-cpu:ewallet-sandbox",
                                       git_sha=SHA, image=NEW, request_id=f"rid-{agent}", post=fake_post(seen=seen))
        assert result.verdict == "PASS", result.reasons
        assert result.observed.get("fraud_probability" if agent == "feature-rag-specialist" else "psi") is not None
        assert result.side_effects and result.endpoint.endswith(f"/api/a2a-sandboxes/ewallet-sandbox/{agent}-cicd/")
    assert {header for _, header, _ in seen} == {"service:ewallet-cicd-candidate"}


@pytest.mark.parametrize(("reply", "verdict"), [
    (lambda rid: specialist_reply("feature-rag-specialist", rid, fraud_score={"fraud_probability": 0.5}), "KNOWN_ANSWER_MISMATCH"),
    # A stale answer from an earlier execution (its request id) never counts.
    (lambda rid: specialist_reply("feature-rag-specialist", "cicd-20261117t090000-000000-feature-rag-specialist"), "BUSINESS_MISMATCH"),
    (lambda rid: specialist_reply("feature-rag-specialist", rid, citations=[]), "BUSINESS_MISMATCH"),
    (lambda rid: specialist_reply("feature-rag-specialist", rid, debug="x"), "INVALID_VERDICT"),
    (lambda rid: specialist_reply("feature-rag-specialist", rid, prompt_revision="other/v9"), "BUSINESS_MISMATCH"),
    (lambda rid: specialist_reply("feature-rag-specialist", rid, status="error"), "BUSINESS_MISMATCH"),
    (lambda rid: specialist_reply("feature-rag-specialist", rid, status="partial"), "PARTIAL"),
    (lambda rid: specialist_reply("feature-rag-specialist", rid, unavailable=["fraud_scoring"]), "PARTIAL"),
    (lambda rid: specialist_reply("feature-rag-specialist", rid, contract_version="g3-orchestration/v2"), "INVALID_VERDICT"),
    (lambda rid: {"jsonrpc": "2.0", "id": rid, "result": {"kind": "task", "id": "t", "status": {"state": "completed"},
                                                          "artifacts": [{"parts": [{"text": "free text"}]}]}}, "INVALID_VERDICT"),
    (lambda rid: {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "boom"}}, "FAILED_TASK"),
    (lambda rid: {"jsonrpc": "2.0", "id": rid, "result": {"kind": "task", "id": "t", "status": {"state": "failed"}}}, "FAILED_TASK"),
    (lambda rid: {"jsonrpc": "2.0", "id": rid, "result": {"kind": "task", "id": "t", "status": {"state": "working"}}}, "A2A_TIMEOUT"),
])
def test_anything_but_a_completed_known_answer_fails(reply, verdict) -> None:
    result = verify_with(reply)
    assert result.verdict == verdict
    assert result.gate == {"KNOWN_ANSWER_MISMATCH": "REGRESSION", "BUSINESS_MISMATCH": "BUSINESS", "PARTIAL": "BUSINESS",
                           "INVALID_VERDICT": "SCHEMA", "FAILED_TASK": "PROTOCOL", "A2A_TIMEOUT": "PROTOCOL"}[verdict]


def test_numeric_tolerance_absorbs_ulp_noise_but_not_a_real_change() -> None:
    pinned = 0.0639895560144887
    noisy = lambda rid: specialist_reply("feature-rag-specialist", rid, fraud_score={"fraud_probability": pinned * (1 + 4e-16)})  # noqa: E731
    assert verify_with(noisy).verdict == "PASS"
    changed = lambda rid: specialist_reply("feature-rag-specialist", rid, fraud_score={"fraud_probability": pinned * (1 + 1e-6)})  # noqa: E731
    assert verify_with(changed).verdict == "KNOWN_ANSWER_MISMATCH"


def test_drift_psi_is_checked_against_the_pinned_value() -> None:
    wrong = lambda rid: specialist_reply("drift-specialist", rid, drift=[{"psi": 0.2}])  # noqa: E731
    assert verify_with(wrong, agent="drift-specialist").verdict == "KNOWN_ANSWER_MISMATCH"
    assert verify_with(lambda rid: specialist_reply("drift-specialist", rid), agent="drift-specialist").verdict == "PASS"


def test_transport_errors_are_failures_not_passes() -> None:
    def refuse(rid):  # type: ignore[no-untyped-def]
        raise ConnectionRefusedError("no route")
    result = verify_with(refuse)
    assert (result.verdict, result.gate) == ("TRANSPORT_ERROR", "TRANSPORT")


def test_evidence_fields_match_the_frozen_specialist_contract() -> None:
    model = pytest.importorskip("ai_platform.llm_agent.programmatic_coordinator").SpecialistEvidence
    assert set(model.model_fields) == specialist_e2e.EVIDENCE_FIELDS


def test_pinned_prompt_revisions_are_the_frozen_code_constants() -> None:
    """Read from source text (no import): the contract pins what the frozen Specialists report."""
    for agent, module in (("feature-rag-specialist", "byo_feature_rag_specialist"), ("drift-specialist", "byo_drift_specialist")):
        source = (ROOT / f"ai_platform/llm_agent/{module}.py").read_text()
        assert f'PROMPT_REVISION = "{CONTRACT["agents"][agent]["expect"]["prompt_revision"]}"' in source


# --- candidate executor -------------------------------------------------------------------------------------
def test_candidate_lifecycle_creates_verifies_and_always_removes_what_it_created() -> None:
    kube = FakeKube()
    before = {k: copy.deepcopy(v) for k, v in kube.objects.items() if k[0] == "SandboxAgent"}
    report = run_candidate(kube)
    assert report.status == "PASS", report.problems
    assert [c[0] for c in kube.calls] == ["create", "create", "delete", "delete"]
    assert {c[1] for c in kube.calls if c[0] == "create"} == {"feature-rag-specialist-cicd", "drift-specialist-cicd"}
    assert all(item["verdict"] == "PASS" for item in report.e2e) and len(report.e2e) == 2
    assert report.cleanup == {"feature-rag-specialist-cicd": "DELETED", "drift-specialist-cicd": "DELETED"}
    assert report.production_unchanged is True
    assert {k: v for k, v in kube.objects.items() if k[0] == "SandboxAgent"} == before
    assert not [k for k in kube.objects if k[0] == "ActorTemplate" and "-cicd-" in k[2]]
    assert all(c["golden_actor"] == "golden-1" for c in report.candidates.values())


def test_without_execute_the_candidate_is_checked_and_nothing_is_created() -> None:
    kube = FakeKube()
    report = run_candidate(kube, execute=False)
    assert report.status == "CHECKED_NOT_EXECUTED" and kube.calls == []
    assert report.candidates["drift-specialist"]["snapshot"].startswith("gs://ate-snapshots/ewallet-sandbox/cicd-")


@pytest.mark.parametrize(("kube", "code"), [
    (lambda: FakeKube(crds=False), "MISSING_CRD"),
    (lambda: FakeKube(pool=None), "MISSING_WORKER_POOL"),
    (lambda: FakeKube(pool={"spec": {"sandboxClass": "runc"}, "status": {"replicas": 2}}), "WORKER_POOL_NOT_READY"),
    (lambda: FakeKube(mcp_ready=False), "MISSING_MCP"),
    (lambda: FakeKube(secrets=False), "MISSING_CREDENTIALS"),
    (lambda: FakeKube(accounts=False), "MISSING_CREDENTIALS"),
    (lambda: FakeKube(fleet=False), "MISSING_PRODUCTION_FLEET"),
])
def test_preflight_refusals_create_nothing(kube, code) -> None:
    fake = kube()
    report = run_candidate(fake)
    assert (report.status, report.failure_code) == ("BLOCKED", code), report.problems
    assert fake.calls == []


def test_a_leftover_candidate_is_never_overwritten_or_adopted() -> None:
    kube = FakeKube()
    kube._admit(sa.render_candidate(CONTRACT, "drift-specialist", NEW, "d" * 40, "20261117t090000-000000"), ready=True)
    report = run_candidate(kube)
    assert report.failure_code == "STALE_CANDIDATE" and kube.calls == []
    assert "20261117t090000-000000" in report.problems[0] and "uid-" in report.problems[0]  # identity for recovery


@pytest.mark.parametrize(("plan", "candidate", "code"), [
    (production_plan(unit()) | {"stage": "rehearsal"}, unit(), "WRONG_TARGET"),
    (production_plan(unit()), unit(target_namespace="ewallet-agents"), "WRONG_TARGET"),
])
def test_wrong_stage_or_namespace_is_refused(plan, candidate, code) -> None:
    kube = FakeKube()
    report = run_candidate(kube, plan=plan, candidate=candidate)
    assert report.failure_code == code and kube.calls == []


@pytest.mark.parametrize(("data", "candidate"), [
    (manifest(sha="d" * 40), unit()),                                       # stale SHA (artifact provenance)
    (manifest(digest=f"{REG}/g3-byo-coordinator@sha256:" + "f" * 64), unit()),  # wrong digest
])
def test_identity_mismatch_is_refused_before_anything_is_created(data, candidate) -> None:
    kube = FakeKube()
    report = run_candidate(kube, data=data, candidate=candidate)
    assert report.failure_code == "IDENTITY_MISMATCH" and kube.calls == []


@pytest.mark.parametrize(("behavior", "code"), [("rejected", "NOT_ACCEPTED"), ("pending", "NOT_READY"),
                                                ("actor-fails", "ACTOR_STARTUP_FAILED")])
def test_runtime_failures_still_clean_up(behavior, code) -> None:
    kube = FakeKube(behavior=behavior)
    report = run_candidate(kube)
    assert report.failure_code == code, report.problems
    assert [c[0] for c in kube.calls].count("delete") == 2
    assert set(report.cleanup.values()) == {"DELETED"} and report.e2e == []


def test_an_a2a_timeout_or_a_wrong_answer_fails_and_cleans_up() -> None:
    working = lambda rid: {"jsonrpc": "2.0", "id": rid, "result": {"kind": "task", "id": "t", "status": {"state": "working"}}}  # noqa: E731
    kube = FakeKube()
    report = run_candidate(kube, post=fake_post({"drift-specialist": working}))
    assert report.failure_code == "E2E_FAILED" and "A2A_TIMEOUT" in report.problems[0]
    assert set(report.cleanup.values()) == {"DELETED"}
    kube = FakeKube()
    wrong = lambda rid: specialist_reply("feature-rag-specialist", rid, fraud_score={"fraud_probability": 0.9})  # noqa: E731
    report = run_candidate(kube, post=fake_post({"feature-rag-specialist": wrong}))
    assert report.failure_code == "E2E_FAILED" and "KNOWN_ANSWER_MISMATCH" in report.problems[0]


@pytest.mark.parametrize("kube", [lambda: FakeKube(delete_fails=True), lambda: FakeKube(delete_sticks=True)])
def test_a_cleanup_failure_is_its_own_verdict(kube) -> None:
    report = run_candidate(kube())
    assert report.status == "CLEANUP_INCOMPLETE" and report.failure_code == "CLEANUP_INCOMPLETE"
    left = {(r["kind"], r["name"]) for r in report.cleanup_incomplete}
    assert ("SandboxAgent", "drift-specialist-cicd") in left and ("SandboxAgent", "feature-rag-specialist-cicd") in left
    assert all(r.get("uid") or r.get("owner_uid") for r in report.cleanup_incomplete)  # exact identities


def test_a_partial_creation_cleans_up_the_candidate_that_was_created() -> None:
    kube = FakeKube(create_fails_for="feature-rag-specialist-cicd")
    report = run_candidate(kube)
    assert report.failure_code == "CANDIDATE_ERROR"
    assert report.cleanup == {"drift-specialist-cicd": "DELETED"} and report.cleanup_incomplete == []
    assert ("SandboxAgent", "ewallet-sandbox", "drift-specialist-cicd") not in kube.objects


def test_a_lost_create_response_is_resolved_by_the_execution_label() -> None:
    kube = FakeKube(create_lost_for="drift-specialist-cicd")
    events: list = []
    report = sandbox_candidate.run(kube, unit(), production_plan(unit()), manifest(), execute=True, forward=loopback,
                                   contract=CONTRACT, post=fake_post(), execution_id=EXEC, emit=events.append,
                                   sleep=lambda s: None)
    assert report.failure_code == "CANDIDATE_ERROR"
    assert report.cleanup["drift-specialist-cicd"] == "DELETED"  # adopted only because it carries this execution
    assert any(e.get("recovered") for e in events)
    assert not [k for k in kube.objects if k[0] == "SandboxAgent" and k[2].endswith("-cicd")]


def test_cleanup_never_deletes_an_object_it_does_not_own() -> None:
    kube = FakeKube()
    document = sa.render_candidate(CONTRACT, "drift-specialist", NEW, SHA, EXEC)
    created = kube.create(document)
    # Someone deleted it and created another object with the same name: another uid.
    del kube.objects[("SandboxAgent", "ewallet-sandbox", "drift-specialist-cicd")]
    kube._admit(sa.render_candidate(CONTRACT, "drift-specialist", NEW, SHA, "20261118t111111-ffffff"), ready=True)
    state, remaining = sandbox_candidate.cleanup(kube, CONTRACT, {"drift-specialist-cicd": created["metadata"]["uid"]},
                                                 EXEC, sleep=lambda s: None)
    assert state == {"drift-specialist-cicd": "NOT_OURS_LEFT_UNTOUCHED"} and remaining == []
    assert not [c for c in kube.calls if c[0] == "delete"]
    assert ("SandboxAgent", "ewallet-sandbox", "drift-specialist-cicd") in kube.objects


def test_cleanup_retries_through_api_timeouts_and_is_idempotent() -> None:
    kube = FakeKube(list_errors=2)
    report = run_candidate(kube)
    assert report.status == "PASS" and set(report.cleanup.values()) == {"DELETED"}
    created = {name: "uid-x" for name in report.cleanup}
    state, remaining = sandbox_candidate.cleanup(kube, CONTRACT, created, EXEC, sleep=lambda s: None)
    assert remaining == [] and set(state.values()) == {"DELETED"}  # already gone counts as deleted


def test_an_unexpected_exception_in_cleanup_is_recorded_not_hidden(monkeypatch) -> None:
    def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("cleanup bug")
    monkeypatch.setattr(sandbox_candidate, "cleanup", explode)
    report = run_candidate(FakeKube())
    assert report.status == "CLEANUP_INCOMPLETE" and len(report.cleanup_incomplete) == 2


def test_the_port_forward_reads_its_own_announced_port(monkeypatch) -> None:
    """A fixed port could be held by another listener; kubectl's own announcement is the only source."""

    import io
    import socket as socket_module

    listener = socket_module.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    class Child:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            self.argv, self.terminated, self.waited = argv, False, False
            self.stdout = io.StringIO(f"Forwarding from 127.0.0.1:{port} -> 8083\n")
            Child.instance = self

        def poll(self):  # type: ignore[no-untyped-def]
            return None

        def terminate(self):  # type: ignore[no-untyped-def]
            self.terminated = True

        def wait(self, timeout=None):  # type: ignore[no-untyped-def]
            self.waited = True
            return 0

    monkeypatch.setattr(sandbox_candidate.subprocess, "Popen", Child)
    monkeypatch.setattr(sandbox_candidate, "_forwarded_port",
                        lambda process, remote, timeout: int(sandbox_candidate.FORWARD_LINE.search(
                            process.stdout.readline()).group(1)))
    try:
        with sandbox_candidate.kubectl_forward("gke-prod", CONTRACT) as endpoint:
            assert endpoint == f"http://127.0.0.1:{port}/"
            assert ":8083" in Child.instance.argv and "--address" in Child.instance.argv
            assert not any(arg.startswith("18") and ":" in arg for arg in Child.instance.argv)  # no fixed local port
        assert Child.instance.terminated and Child.instance.waited  # terminated and reaped
    finally:
        listener.close()


def test_a_change_to_a_production_sandbox_agent_during_the_run_is_reported() -> None:
    report = run_candidate(FakeKube(touch_production=True))
    assert report.failure_code == "PRODUCTION_CHANGED" and report.production_unchanged is False


def test_candidate_evidence_is_written(tmp_path) -> None:
    for _ in range(2):  # two executions of the same SHA: two fresh evidence sets, distinct request ids
        sandbox_candidate.run(FakeKube(), unit(), production_plan(unit()), manifest(), execute=True, forward=loopback,
                              contract=CONTRACT, post=fake_post(), evidence=tmp_path, sleep=lambda s: None)
    runs = sorted(tmp_path.glob("*/candidate-verdict.json"))
    assert len(runs) == 2
    data = [json.loads(path.read_text()) for path in runs]
    assert all(d["status"] == "PASS" and d["e2e"][0]["git_sha"] == SHA and d["e2e"][0]["image"] == NEW for d in data)
    assert data[0]["execution_id"] != data[1]["execution_id"]
    assert data[0]["e2e"][0]["request_id"] != data[1]["e2e"][0]["request_id"]
    assert data[0]["snapshot_prefix"] != data[1]["snapshot_prefix"]


# --- fleet promotion ------------------------------------------------------------------------------------------
def promote_fleet(kube: FakeKube, execute: bool = True) -> tuple[list, Exception | None]:
    record: list = []
    try:
        fleet_promote.promote(kube, unit("fleet-image-promotion"), execute=execute, record=record, contract=CONTRACT,
                              clock=iter(range(0, 100000, 7)).__next__, sleep=lambda s: None)
    except Exception as error:  # promotion or API errors; the transaction catches every exception the same way
        return record, error
    return record, None


def test_fleet_promotion_patches_only_the_image_with_uid_and_image_tests() -> None:
    kube = FakeKube()
    before = kube.get("SandboxAgent", "ewallet-sandbox", "drift-specialist")
    record, error = promote_fleet(kube)
    assert error is None and [r.status for r in record] == ["PROMOTED", "PROMOTED"]
    assert [r.agent for r in record] == ["feature-rag-specialist", "drift-specialist"]
    after = kube.get("SandboxAgent", "ewallet-sandbox", "drift-specialist")
    assert after["spec"]["byo"]["deployment"]["image"] == NEW
    after["spec"]["byo"]["deployment"]["image"] = OLD
    assert after["spec"] == before["spec"] and after["metadata"]["uid"] == before["metadata"]["uid"]
    assert after["metadata"].get("labels") == before["metadata"].get("labels")
    operations = json.loads([c for c in kube.calls if c[0] == "patch"][0][2])
    assert [op["op"] for op in operations] == ["test", "test", "replace"]
    assert [op["path"] for op in operations] == ["/metadata/uid", fleet_promote.IMAGE_PATH, fleet_promote.IMAGE_PATH]


def test_fleet_promotion_is_a_no_op_when_current_and_checks_without_execute() -> None:
    kube = FakeKube()
    record, _ = promote_fleet(kube, execute=False)
    assert [r.status for r in record] == ["CHECKED_NOT_EXECUTED"] * 2 and kube.calls == []
    promote_fleet(kube)
    record, _ = promote_fleet(kube)
    assert [r.status for r in record] == ["ALREADY_CURRENT"] * 2


def test_an_unhealthy_or_missing_fleet_is_refused_before_any_patch() -> None:
    kube = FakeKube()
    kube.objects[("SandboxAgent", "ewallet-sandbox", "feature-rag-specialist")]["status"]["conditions"][1]["status"] = "False"
    record, error = promote_fleet(kube)
    assert "not healthy before promotion" in str(error) and record[0].status == "REFUSED"
    assert not [c for c in kube.calls if c[0] == "patch"]
    record, error = promote_fleet(FakeKube(fleet=False))
    assert "missing" in str(error)


def test_a_fleet_that_does_not_become_ready_is_patched_back() -> None:
    kube = FakeKube(behavior="patch-not-ready")
    record, error = promote_fleet(kube)
    assert "did not become Ready" in str(error)
    errors = fleet_promote.rollback(kube, CONTRACT, record, clock=iter(range(0, 100000, 7)).__next__, sleep=lambda s: None)
    assert errors == [] and record[0].status == "ROLLED_BACK"
    assert kube.get("SandboxAgent", "ewallet-sandbox", "feature-rag-specialist")["spec"]["byo"]["deployment"]["image"] == OLD


def test_first_specialist_patched_second_fails_rolls_back_the_first() -> None:
    kube = FakeKube(patch_fails_for="drift-specialist")
    record, error = promote_fleet(kube)
    assert error is not None and [r.status for r in record] == ["PROMOTED", "PATCH_ATTEMPTED"]
    errors = fleet_promote.rollback(kube, CONTRACT, record, clock=iter(range(0, 100000, 7)).__next__, sleep=lambda s: None)
    assert errors == [] and [r.status for r in record] == ["ROLLED_BACK", "NOT_CHANGED"]
    assert kube.get("SandboxAgent", "ewallet-sandbox", "feature-rag-specialist")["spec"]["byo"]["deployment"]["image"] == OLD


def test_a_lost_patch_response_is_inspected_and_rolled_back() -> None:
    kube = FakeKube(patch_lost_for="feature-rag-specialist")
    record, error = promote_fleet(kube)
    assert error is not None and record[0].status == "PATCH_ATTEMPTED"
    errors = fleet_promote.rollback(kube, CONTRACT, record, clock=iter(range(0, 100000, 7)).__next__, sleep=lambda s: None)
    assert errors == [] and record[0].status == "ROLLED_BACK"
    assert kube.get("SandboxAgent", "ewallet-sandbox", "feature-rag-specialist")["spec"]["byo"]["deployment"]["image"] == OLD


def test_a_recreated_agent_is_never_overwritten_by_the_rollback() -> None:
    kube = FakeKube(patch_lost_for="feature-rag-specialist")
    record, _ = promote_fleet(kube)
    kube.objects[("SandboxAgent", "ewallet-sandbox", "feature-rag-specialist")]["metadata"]["uid"] = "uid-recreated"
    errors = fleet_promote.rollback(kube, CONTRACT, record, clock=iter(range(0, 100000, 7)).__next__, sleep=lambda s: None)
    assert errors and "changed concurrently" in errors[0] and record[0].status == "ROLLBACK_FAILED"
    assert len([c for c in kube.calls if c[0] == "patch"]) == 1  # only the original attempt


def test_a_rollback_that_never_becomes_ready_is_a_rollback_failure() -> None:
    kube = FakeKube(behavior="patch-not-ready", rollback_never_ready=True)
    record, _ = promote_fleet(kube)
    errors = fleet_promote.rollback(kube, CONTRACT, record, clock=iter(range(0, 100000, 7)).__next__, sleep=lambda s: None)
    assert errors and "not Ready" in errors[0] and record[0].status == "ROLLBACK_NOT_READY"


def test_a_concurrent_change_makes_the_rollback_fail_loudly() -> None:
    kube = FakeKube(behavior="patch-not-ready")
    record, _ = promote_fleet(kube)
    kube.objects[("SandboxAgent", "ewallet-sandbox", "feature-rag-specialist")]["spec"]["byo"]["deployment"]["image"] = "x"
    errors = fleet_promote.rollback(kube, CONTRACT, record, clock=iter(range(0, 100000, 7)).__next__, sleep=lambda s: None)
    assert errors and "rollback failed" in errors[0] and record[0].status == "ROLLBACK_FAILED"


# --- RBAC proposal is inert and bounded -----------------------------------------------------------------------
def test_the_agent_cicd_rbac_proposal_is_inert_and_scoped() -> None:
    path = ROOT / "infra/kubernetes/jenkins/rbac/agent-cicd-production.proposed.yaml"
    documents = [d for d in yaml.safe_load_all(path.read_text()) if d]
    assert all(d["metadata"]["annotations"]["ewallet.io/status"] == "proposed-not-applied" for d in documents)
    rules = [(d["metadata"]["namespace"], r) for d in documents if d["kind"] == "Role" for r in d["rules"]]
    for namespace, rule in rules:
        assert "*" not in rule["verbs"] and "*" not in rule["resources"]
        if "secrets" in rule["resources"] or "serviceaccounts" in rule["resources"]:
            assert rule["verbs"] == ["get"] and rule.get("resourceNames")
        if "workerpools" in rule["resources"] or "actortemplates" in rule["resources"]:
            assert set(rule["verbs"]) <= {"get", "list", "watch"}  # the WorkerPool is never written
    assert {ns for ns, _ in rules} <= {"ewallet-sandbox", "ewallet-mcp", "kagent", "ewallet-cicd"}


# --- artifact identity: the fleet image -------------------------------------------------------------------------
def test_the_fleet_image_build_context_contains_both_specialists_and_their_actor_contract() -> None:
    """The fleet is promoted with the g3-byo-coordinator image. Its Dockerfile copies the whole
    ai_platform/llm_agent package into /workspace (the actor's PYTHONPATH) and the locked venv into
    /opt/venv (the actor's cmd), so both Specialist modules ship with their dependencies. A local build
    and an import of both modules with the actor environment is recorded in the runbook (§9)."""

    dockerfile = (ROOT / "infra/docker/g3-byo-coordinator/Dockerfile").read_text()
    assert "COPY ai_platform/llm_agent ai_platform/llm_agent" in dockerfile
    assert "WORKDIR /workspace" in dockerfile and "COPY --from=deps /opt/venv /opt/venv" in dockerfile
    assert "uv sync --locked" in dockerfile
    catalog = json.loads((ROOT / "jenkins/catalog/components.json").read_text())
    images = {i["name"]: i for c in catalog["components"].values() for i in c.get("images", [])}
    assert images["g3-byo-coordinator"]["dockerfile"] == "infra/docker/g3-byo-coordinator/Dockerfile"
    for agent, spec in CONTRACT["agents"].items():
        module_path = ROOT / (spec["module"].replace(".", "/") + ".py")
        assert module_path.is_file() and module_path.is_relative_to(ROOT / "ai_platform/llm_agent")
        frozen = sa.frozen_documents(NEW, CONTRACT["credential_release"])[agent]["spec"]["byo"]["deployment"]
        assert frozen["cmd"] == "/opt/venv/bin/python" and frozen["args"] == ["-m", spec["module"]]
        assert {"name": "PYTHONPATH", "value": "/workspace"} in frozen["env"]


def test_both_specialists_import_in_the_locked_image_environment() -> None:
    """Runs only where the g3-byo-coordinator locked environment is present (CD_REHEARSAL_AGENT_PYTHON)."""

    import os
    import subprocess

    python = os.environ.get("CD_REHEARSAL_AGENT_PYTHON")
    if not python:
        pytest.skip("needs the g3-byo-coordinator environment (set CD_REHEARSAL_AGENT_PYTHON)")
    for spec in CONTRACT["agents"].values():
        result = subprocess.run([python, "-c", f"import {spec['module']} as m; print(m.APP_NAME, m.PROMPT_REVISION)"],
                                cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr[-400:]
        assert spec["expect"]["prompt_revision"] in result.stdout
