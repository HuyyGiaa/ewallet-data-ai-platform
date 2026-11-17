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
                 delete_fails: bool = False, delete_sticks: bool = False, touch_production: bool = False) -> None:
        self.crds, self.mcp_ready, self.behavior = crds, mcp_ready, behavior
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
        return [copy.deepcopy(v) for (k, n, _), v in self.objects.items() if k == kind and n == namespace]

    def create(self, document: dict) -> dict:
        self.calls.append(("create", document["metadata"]["name"]))
        key = ("SandboxAgent", document["metadata"]["namespace"], document["metadata"]["name"])
        if key in self.objects:
            raise KubeError("POST -> HTTP 409: AlreadyExists")
        item = self._admit(document, ready=self.behavior == "ready")
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
        item = self.objects[(kind, namespace, name)]
        for op in operations:
            target = {"/metadata/uid": item["metadata"]["uid"],
                      fleet_promote.IMAGE_PATH: item["spec"]["byo"]["deployment"]["image"]}[op["path"]]
            if op["op"] == "test" and target != op["value"]:
                raise KubeError("PATCH -> HTTP 422: test operation failed")
        image = operations[-1]["value"]
        item["spec"]["byo"]["deployment"]["image"] = image
        item["metadata"]["generation"] += 1
        ready = not (self.behavior == "patch-not-ready" and image == NEW)
        self._status(item, ready)
        self._template(item, ready)
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
                                 clock=lambda: next(ticks), sleep=lambda s: None)


# --- rendering: the frozen deployment contract -----------------------------------------------------------------
def test_candidate_differs_from_the_frozen_fleet_only_in_name_labels_image_and_snapshot_prefix() -> None:
    for agent in sa.production_agents(CONTRACT):
        candidate = sa.render_candidate(CONTRACT, agent, NEW, SHA)
        frozen = sa.frozen_documents(NEW, CONTRACT["credential_release"])[agent]
        assert candidate["metadata"]["name"] == f"{agent}-cicd"
        assert candidate["metadata"]["namespace"] == "ewallet-sandbox"
        assert candidate["metadata"]["labels"][sa.RELEASE_SHA_LABEL] == SHA
        assert candidate["spec"]["substrate"]["snapshotsConfig"]["location"] == \
            f"gs://ate-snapshots/ewallet-sandbox/cicd-{SHA[:12]}/{agent}-cicd/"
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
    candidate = sa.render_candidate(CONTRACT, "drift-specialist", NEW, SHA)
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
    for args, message in (((CONTRACT, "fraud-coordinator", NEW, SHA), "not a production Specialist"),
                          ((CONTRACT, "drift-specialist", f"{REG}/g3:latest", SHA), "immutable digest"),
                          ((CONTRACT, "drift-specialist", NEW, "abc"), "full lowercase SHA")):
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
    (lambda rid: specialist_reply("feature-rag-specialist", "other"), "KNOWN_ANSWER_MISMATCH"),
    (lambda rid: specialist_reply("feature-rag-specialist", rid, citations=[]), "KNOWN_ANSWER_MISMATCH"),
    (lambda rid: specialist_reply("feature-rag-specialist", rid, debug="x"), "KNOWN_ANSWER_MISMATCH"),
    (lambda rid: specialist_reply("feature-rag-specialist", rid, prompt_revision="other/v9"), "KNOWN_ANSWER_MISMATCH"),
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
    assert verify_with(reply).verdict == verdict


def test_drift_psi_is_checked_against_the_pinned_value() -> None:
    wrong = lambda rid: specialist_reply("drift-specialist", rid, drift=[{"psi": 0.2}])  # noqa: E731
    assert verify_with(wrong, agent="drift-specialist").verdict == "KNOWN_ANSWER_MISMATCH"
    assert verify_with(lambda rid: specialist_reply("drift-specialist", rid), agent="drift-specialist").verdict == "PASS"


def test_transport_errors_are_failures_not_passes() -> None:
    def refuse(rid):  # type: ignore[no-untyped-def]
        raise ConnectionRefusedError("no route")
    assert verify_with(refuse).verdict == "TRANSPORT_ERROR"


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
    kube._admit(sa.render_candidate(CONTRACT, "drift-specialist", NEW, "d" * 40), ready=True)
    report = run_candidate(kube)
    assert report.failure_code == "STALE_CANDIDATE" and kube.calls == []


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
    assert report.status == "CLEANUP_FAILED"
    assert any("still present after cleanup" in p for p in report.problems)


def test_a_change_to_a_production_sandbox_agent_during_the_run_is_reported() -> None:
    report = run_candidate(FakeKube(touch_production=True))
    assert report.failure_code == "PRODUCTION_CHANGED" and report.production_unchanged is False


def test_candidate_evidence_is_written(tmp_path) -> None:
    sandbox_candidate.run(FakeKube(), unit(), production_plan(unit()), manifest(), execute=True, forward=loopback,
                          contract=CONTRACT, post=fake_post(), evidence=tmp_path, sleep=lambda s: None)
    data = json.loads((tmp_path / "candidate-verdict.json").read_text())
    assert data["status"] == "PASS" and data["e2e"][0]["git_sha"] == SHA and data["e2e"][0]["image"] == NEW


# --- fleet promotion ------------------------------------------------------------------------------------------
def promote_fleet(kube: FakeKube, execute: bool = True) -> tuple[list, Exception | None]:
    record: list = []
    try:
        fleet_promote.promote(kube, unit("fleet-image-promotion"), execute=execute, record=record, contract=CONTRACT,
                              clock=iter(range(0, 100000, 7)).__next__, sleep=lambda s: None)
    except fleet_promote.FleetPromotionError as error:
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


def test_a_concurrent_change_makes_the_rollback_fail_loudly() -> None:
    kube = FakeKube(behavior="patch-not-ready")
    record, _ = promote_fleet(kube)
    kube.objects[("SandboxAgent", "ewallet-sandbox", "feature-rag-specialist")]["spec"]["byo"]["deployment"]["image"] = "x"
    errors = fleet_promote.rollback(kube, CONTRACT, record, clock=iter(range(0, 100000, 7)).__next__, sleep=lambda s: None)
    assert errors and "rollback failed" in errors[0] and record[0].status == "ROLLBACK_FAILED"


# --- production transaction -----------------------------------------------------------------------------------
def full_stack_plan() -> dict:
    units = json.loads((ROOT / "jenkins/catalog/release-units.json").read_text())
    planned = []
    for name in units["groups"]["release/coordinator"]["production"]:
        item = {**units["units"][name], "name": name}
        item["images"] = {image: (MCP_NEW if image == "mcp-tools" else NEW) for image in item["images"]}
        planned.append(item)
    return production_plan(*planned)


def transaction(kube: FakeKube, helm: FakeCluster, verify=lambda expected: True, execute: bool = True, post=None):  # type: ignore[no-untyped-def]
    original = sandbox_candidate.run

    def run(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.update(contract=CONTRACT, post=post or fake_post(), sleep=lambda s: None,
                      clock=iter(range(0, 100000, 7)).__next__)
        return original(*args, **kwargs)

    sandbox_candidate.run = run
    original_sleep = fleet_promote.time.sleep
    fleet_promote.time.sleep = lambda s: None
    try:
        return production_release.run(full_stack_plan(), "gke-prod", manifest(), execute=execute, kube=kube,
                                      helm_runner=helm, verify=verify, forward=loopback)
    finally:
        sandbox_candidate.run = original
        fleet_promote.time.sleep = original_sleep


def test_the_full_stack_release_runs_candidate_mcp_fleet_coordinator_and_states_the_expected_runtime(monkeypatch) -> None:
    monkeypatch.setenv("PROMOTION_APPROVED", "yes")
    kube, helm = FakeKube(), FakeCluster()
    seen = {}
    report = transaction(kube, helm, verify=lambda expected: seen.update(expected) or True)
    assert report["verdict"] == "PASS", report["failure"]
    assert report["production_changed"] and report["candidate_objects_created"]
    assert [u["status"] for u in report["units"]["helm"]] == ["UPGRADED", "UPGRADED"]
    assert [u["status"] for u in report["units"]["fleet"]] == ["PROMOTED", "PROMOTED"]
    assert seen["helm_revisions"] == {"llm-agent": 60, "llm-agent-mcp": 20}
    assert seen["images"]["ewallet-sandbox/SandboxAgent/drift-specialist"] == NEW
    assert seen["images"]["ewallet-mcp/Deployment/drift-mcp"] == MCP_NEW
    assert seen["images"]["ewallet-agents/Deployment/fraud-coordinator"] == NEW
    assert report["registry"]["status"] == "PUBLICATION_REQUIRES_APPROVAL"
    assert report["registry"]["changed"] == {"coordinator": True, "sandbox_fleet": True}
    assert "--confirm-live-write" in report["registry"]["commands"][1]  # never run by CI/CD


def test_no_production_stage_gates_rollback_on_the_registry_readback() -> None:
    units = json.loads((ROOT / "jenkins/catalog/release-units.json").read_text())["units"]
    assert not [name for name, item in units.items() if item["stage"] == "production" and "registry-readback" in item["verify"]]


def test_a_failed_candidate_stops_the_release_before_any_promotion(monkeypatch) -> None:
    monkeypatch.setenv("PROMOTION_APPROVED", "yes")
    kube, helm = FakeKube(behavior="actor-fails"), FakeCluster()
    report = transaction(kube, helm)
    assert report["verdict"] == "FAIL" and report["failed_unit"] == "agent-fleet-candidate"
    # The candidate runs first: nothing in production was promoted, so nothing needs a rollback.
    assert report["units"]["helm"] == [] and report["units"]["fleet"] == [] and not report["production_changed"]
    assert not [c for c in helm.calls if c[:2] in (["helm", "upgrade"], ["helm", "rollback"])]
    assert report["candidate_objects_created"] and report["units"]["candidates"][0]["cleanup"]


def test_a_failed_verification_rolls_back_coordinator_fleet_and_mcp_in_reverse(monkeypatch) -> None:
    monkeypatch.setenv("PROMOTION_APPROVED", "yes")
    kube, helm = FakeKube(), FakeCluster()
    report = transaction(kube, helm, verify=lambda expected: False)
    assert report["verdict"] == "FAIL" and report["failure"] == "runtime verification failed after promotion"
    assert [u["status"] for u in report["units"]["helm"]] == ["ROLLED_BACK", "ROLLED_BACK"]
    assert [u["status"] for u in report["units"]["fleet"]] == ["ROLLED_BACK", "ROLLED_BACK"]
    rollbacks = [c[2] for c in helm.calls if c[:2] == ["helm", "rollback"]]
    assert rollbacks == ["llm-agent", "llm-agent-mcp"]
    image = kube.get("SandboxAgent", "ewallet-sandbox", "drift-specialist")["spec"]["byo"]["deployment"]["image"]
    assert image == OLD


def test_a_partial_failure_in_the_coordinator_upgrade_rolls_back_the_fleet_and_mcp(monkeypatch) -> None:
    monkeypatch.setenv("PROMOTION_APPROVED", "yes")
    kube, helm = FakeKube(), FakeCluster(fail_upgrade_of="llm-agent")
    report = transaction(kube, helm)
    assert report["failed_unit"] == "coordinator-production"
    assert [u["status"] for u in report["units"]["fleet"]] == ["ROLLED_BACK", "ROLLED_BACK"]
    assert report["units"]["helm"][0]["status"] == "ROLLED_BACK"


def test_a_rollback_failure_is_the_verdict(monkeypatch) -> None:
    monkeypatch.setenv("PROMOTION_APPROVED", "yes")
    kube, helm = FakeKube(), FakeCluster()

    def verify(expected):  # type: ignore[no-untyped-def]
        # Someone changes the fleet behind the release: the guarded patch-back must refuse.
        kube.objects[("SandboxAgent", "ewallet-sandbox", "drift-specialist")]["spec"]["byo"]["deployment"]["image"] = "x"
        return False

    report = transaction(kube, helm, verify=verify)
    assert report["verdict"] == "ROLLBACK_FAILED" and any("drift-specialist" in e for e in report["rollback_errors"])


def test_without_execute_or_approval_nothing_changes(monkeypatch) -> None:
    monkeypatch.delenv("PROMOTION_APPROVED", raising=False)
    kube, helm = FakeKube(), FakeCluster()
    report = transaction(kube, helm, execute=False)
    assert report["verdict"] == "CHECKED_NOT_EXECUTED" and not report["production_changed"]
    assert kube.calls == [] and not [c for c in helm.calls if c[:2] in (["helm", "upgrade"], ["helm", "rollback"])]


def test_execute_without_the_production_approval_changes_nothing(monkeypatch) -> None:
    monkeypatch.setenv("PROMOTION_APPROVED", "no")
    kube, helm = FakeKube(), FakeCluster()
    verified = []
    report = transaction(kube, helm, verify=lambda expected: verified.append(1) or True)
    assert report["verdict"] == "CHECKED_NOT_EXECUTED" and not report["production_changed"]
    assert kube.calls == [] and verified == []
    assert [u["status"] for u in report["units"]["fleet"]] == ["CHECKED_NOT_EXECUTED"] * 2


@pytest.mark.parametrize("context", ["", "in-cluster"])
def test_the_transaction_needs_an_explicit_production_context(context) -> None:
    with pytest.raises(production_release.ProductionReleaseError):
        production_release.run(full_stack_plan(), context, manifest(), execute=True, kube=FakeKube(),
                               helm_runner=FakeCluster())


# --- runtime verification: target guard and reconciliation ---------------------------------------------------
def test_verification_refuses_a_campaign_env_for_another_cluster(tmp_path) -> None:
    env = tmp_path / "agi.env"
    env.write_text("export CTX=kind-ewallet-local\nEV=/x\n")
    with pytest.raises(runtime_verify.VerificationError, match="TARGET_MISMATCH"):
        runtime_verify.guard_target({"AGI_ENV": str(env)}, "gke-prod")
    with pytest.raises(runtime_verify.VerificationError, match="AGI_ENV"):
        runtime_verify.guard_target({}, "gke-prod")
    env.write_text("CTX='gke-prod'\n")
    runtime_verify.guard_target({"AGI_ENV": str(env)}, "gke-prod")
    with pytest.raises(runtime_verify.VerificationError):
        runtime_verify.guard_target({"AGI_ENV": str(env)}, "in-cluster")


def status_snapshot(tmp_path: Path, *, helm: dict[str, int], frozen_fail: list[str], images: dict[str, str]) -> Path:
    snapshot = tmp_path / "status"
    snapshot.mkdir(parents=True, exist_ok=True)
    rows = [["FAIL" if f"Helm {r}" in frozen_fail else "PASS", f"Helm {r}", f"revision {v} deployed"] for r, v in helm.items()]
    rows += [["FAIL", check, "x"] for check in frozen_fail if not check.startswith("Helm ")]
    (snapshot / "verdict.json").write_text(json.dumps({"verdict": "FAIL" if frozen_fail else "PASS", "rows": rows}))
    for release, revision in helm.items():
        (snapshot / f"helm-{release}.json").write_text(json.dumps([{"revision": revision, "status": "deployed"}]))
    deployments = {"ewallet-agents": [], "ewallet-mcp": []}
    agents = []
    for key, image in images.items():
        namespace, kind, name = key.split("/")
        if kind == "SandboxAgent":
            agents.append({"metadata": {"name": name}, "spec": {"byo": {"deployment": {"image": image}}}})
        else:
            deployments[namespace].append({"metadata": {"name": name},
                                           "spec": {"template": {"spec": {"containers": [{"image": image}]}}}})
    for namespace, items in deployments.items():
        (snapshot / f"deployments-{namespace}.json").write_text(json.dumps({"items": items}))
    (snapshot / "sandboxagents.json").write_text(json.dumps({"items": agents}))
    return snapshot


EXPECTED_IMAGES = {"ewallet-sandbox/SandboxAgent/drift-specialist": NEW, "ewallet-agents/Deployment/fraud-coordinator": NEW}


def test_moved_helm_revisions_reconcile_only_to_this_transactions_revision(tmp_path) -> None:
    status_snapshot(tmp_path, helm={"llm-agent": 61, "llm-agent-mcp": 20}, frozen_fail=["Helm llm-agent", "Helm llm-agent-mcp"],
                    images=EXPECTED_IMAGES)
    rows = runtime_verify.reconcile_status(tmp_path, {"helm_revisions": {"llm-agent": 61, "llm-agent-mcp": 20},
                                                      "images": EXPECTED_IMAGES})
    assert all(row["status"] == "PASS" for row in rows) and any(row["check"].startswith("RECONCILED") for row in rows)
    rows = runtime_verify.reconcile_status(tmp_path, {"helm_revisions": {"llm-agent": 62, "llm-agent-mcp": 20},
                                                      "images": EXPECTED_IMAGES})
    assert [row["check"] for row in rows if row["status"] == "FAIL"] == ["RECONCILED Helm llm-agent"]


def test_any_other_frozen_failure_and_any_wrong_image_still_fail(tmp_path) -> None:
    status_snapshot(tmp_path, helm={"llm-agent": 60}, frozen_fail=["SandboxAgent drift-specialist"],
                    images={**EXPECTED_IMAGES, "ewallet-sandbox/SandboxAgent/drift-specialist": OLD})
    rows = runtime_verify.reconcile_status(tmp_path, {"helm_revisions": {"llm-agent": 60}, "images": EXPECTED_IMAGES})
    failed = {row["check"] for row in rows if row["status"] == "FAIL"}
    assert failed == {"SandboxAgent drift-specialist", "IDENTITY ewallet-sandbox/SandboxAgent/drift-specialist"}


def test_run_checks_uses_the_reconciliation_for_the_status_command(tmp_path) -> None:
    def run(argv, env):  # type: ignore[no-untyped-def]
        args = list(argv[1:])
        if args == ["status"]:
            status_snapshot(Path(env["REPLAY_DIR"]), helm={"llm-agent": 61}, frozen_fail=["Helm llm-agent"],
                            images=EXPECTED_IMAGES)
            return 1  # the frozen verdict fails on the moved revision
        path = Path(env["REPLAY_DIR"]) / "e2e" / f"{args[-1]}-verdict.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"verdict": "COMPLETE"}))
        return 0

    env = tmp_path / "agi.env"
    env.write_text("CTX=gke-prod\n")
    expected = {"helm_revisions": {"llm-agent": 61}, "images": EXPECTED_IMAGES}
    report = runtime_verify.run_checks(["platform-status", "e2e-fraud"], tmp_path / "replay", run=run,
                                       environment={"AGI_ENV": str(env)}, expected=expected, production_context="gke-prod")
    assert report["verdict"] == "PASS" and report["reconciliation"]
    without = runtime_verify.run_checks(["platform-status"], tmp_path / "replay2", run=run, environment={})
    assert without["verdict"] == "FAIL"  # unreconciled, the frozen verdict decides


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
