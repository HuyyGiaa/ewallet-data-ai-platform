"""CI/CD V2-P3A: the agent-staging release unit, its contract, the staging fleet and the executor (offline, fakes only)."""

from __future__ import annotations

import contextlib
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.ci import agent_staging as st
from scripts.ci import sandbox_agents as sa
from scripts.ci.catalog import load_catalog
from scripts.ci.release_dispatch import commands_for
from scripts.ci.release_policy import ReleasePolicyError, ReleaseRequest, build_release_plan, select_units
from scripts.ci.release_units import ReleaseUnitError, load_release_units

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
G3 = "asia-southeast1-docker.pkg.dev/p/r/g3-byo-coordinator@sha256:" + "b" * 64
MCP = "asia-southeast1-docker.pkg.dev/p/r/mcp-tools@sha256:" + "c" * 64
MANIFEST = {"git_sha": SHA, "images": {"g3-byo-coordinator": {"digest_ref": G3, "revision": SHA},
                                        "mcp-tools": {"digest_ref": MCP, "revision": SHA}}}
EXECUTION = "20261118t120000-abc123"
CONTRACT = st.load_contract()
UNIT = {"name": "agent-staging", "target_namespace": st.AGENTS_NAMESPACE, "images": {"g3-byo-coordinator": G3, "mcp-tools": MCP}}
PLAN = {"stage": "rehearsal", "git_sha": SHA, "environment": "gke-cpu"}
SCHEMAS = json.loads((ROOT / "infra/kubernetes/kagent-platform/crd-schemas-kagent-0.10.3.json").read_text())


def request(**changes) -> ReleaseRequest:
    values = dict(requested=True, owner_approved=True, owner_approval="owner", branch="release/agent-staging", git_sha=SHA,
                  checkout_sha=SHA, environment="gke-cpu", kube_context="in-cluster", components=("agents",),
                  image_manifest_path="build/image-manifest.json", evidence_dir="build/release")
    values.update(changes)
    return ReleaseRequest(**values)


class ReleaseUnitTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog(ROOT / "jenkins/catalog/components.json")
        self.units = load_release_units(ROOT / "jenkins/catalog/release-units.json")
        self.units.validate(ROOT, self.catalog)

    def test_explicit_release_selects_only_the_staging_unit(self):
        self.assertEqual(select_units(self.units, request(), self.catalog), ["agent-staging"])
        plan = build_release_plan(self.catalog, self.units, request(), MANIFEST, "d" * 64)
        self.assertEqual(plan["units"][0]["images"], UNIT["images"])
        self.assertEqual(plan["units"][0]["target_namespace"], "ewallet-agents-staging")

    def test_staging_never_deploys_without_explicit_approval_or_digest(self):
        for label, changes in {"not requested": {"requested": False}, "no approval": {"owner_approved": False},
                               "not a release branch": {"branch": "feature/edai2-cicd-v2"},
                               "other release group": {"branch": "release/coordinator", "components": ("prompt-management",)}}.items():
            with self.subTest(label), self.assertRaises(ReleasePolicyError):
                select_units(self.units, request(**changes), self.catalog)
        without_mcp = copy.deepcopy(MANIFEST)
        del without_mcp["images"]["mcp-tools"]
        with self.assertRaises(ReleasePolicyError):
            build_release_plan(self.catalog, self.units, request(), without_mcp, "d" * 64)
        tag = copy.deepcopy(MANIFEST)
        tag["images"]["mcp-tools"]["digest_ref"] = "asia-southeast1-docker.pkg.dev/p/r/mcp-tools:latest"
        with self.assertRaises(ReleasePolicyError):
            build_release_plan(self.catalog, self.units, request(), tag, "d" * 64)

    def test_a_staging_unit_cannot_target_production(self):
        raw = json.loads((ROOT / "jenkins/catalog/release-units.json").read_text())
        for namespace in ("ewallet-agents", "ewallet-sandbox", "ewallet-cicd"):
            raw["units"]["agent-staging"]["target_namespace"] = namespace
            with tempfile.TemporaryDirectory() as tmp, self.subTest(namespace):
                path = Path(tmp) / "units.json"
                path.write_text(json.dumps(raw))
                with self.assertRaises(ReleaseUnitError):
                    load_release_units(path).validate(ROOT, self.catalog)

    def test_dispatch_runs_the_staging_executor_with_the_plan_identity(self):
        plan = build_release_plan(self.catalog, self.units, request(), MANIFEST, "d" * 64)
        [command] = commands_for(plan["units"][0], plan, Path("/evidence"))
        self.assertIn("scripts.ci.agent_staging", command)
        for flag, value in (("--git-sha", SHA), ("--target-namespace", "ewallet-agents-staging"),
                            ("--contract", "jenkins/cd/agent-staging.contract.json"), ("--kube-context", "in-cluster")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn(f"g3-byo-coordinator={G3}", command)

    def test_contract_refuses_production_targets_and_shared_credentials(self):
        raw = json.loads((ROOT / "jenkins/cd/agent-staging.contract.json").read_text())
        mutations = {"production namespace": ("agents_namespace", "ewallet-agents"),
                     "production sandbox": ("sandbox_namespace", "ewallet-sandbox"),
                     "production snapshot prefix": ("snapshot_prefix", "gs://ate-snapshots/ewallet-sandbox/")}
        for label, (key, value) in mutations.items():
            with tempfile.TemporaryDirectory() as tmp, self.subTest(label):
                path = Path(tmp) / "c.json"
                path.write_text(json.dumps({**raw, key: value}))
                with self.assertRaises(st.StagingContractError):
                    st.load_contract(path)
        shared = copy.deepcopy(raw)
        shared["agents"]["drift-specialist"]["credential_secret"] = "mcp-token-sandbox-drift-drift-mcp-sbx-20261115"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps(shared))
            with self.assertRaises(st.StagingContractError):
                st.load_contract(path)


class InventoryTests(unittest.TestCase):
    def test_contract_requires_exactly_the_level_a_credential_inventory(self):
        from ops.llm_agent.staging_credentials import LLM_ONLY, SECRETS

        required = {(ns, name) for ns, scope in CONTRACT["required"].items() for name in scope["secrets"]}
        self.assertEqual(required, {(s.namespace, s.name) for s in SECRETS})
        self.assertEqual(len(required), 10)
        self.assertEqual(CONTRACT["dependencies"]["llm_only_secrets"], [s.name for s in LLM_ONLY])

    def test_dependency_pins_match_the_manifests(self):
        import yaml

        images = set()
        for name in ("feast-redis.yaml", "fraud-predictor.yaml"):
            for doc in yaml.safe_load_all((ROOT / "infra/kubernetes/agent-staging" / name).read_text()):
                self.assertEqual(doc["metadata"]["namespace"], st.AGENTS_NAMESPACE)
                pod = doc.get("spec", {}).get("template", doc.get("spec", {}).get("predictor", {}))
                images |= {c["image"] for c in (pod.get("spec", pod).get("containers") or [])}
        self.assertEqual(images, {d["image"] for d in CONTRACT["dependencies"]["deployments"]})


class FleetTests(unittest.TestCase):
    def test_fleet_validates_against_the_pinned_crds_and_stays_in_staging(self):
        import jsonschema

        documents = st.render_fleet(CONTRACT, G3, SHA, EXECUTION)
        self.assertEqual([(d["kind"], d["metadata"]["name"]) for d in documents],
                         [("WorkerPool", "staging-agents"), ("ServiceAccount", "sandbox-feature-rag"), ("ServiceAccount", "sandbox-drift"),
                          ("SandboxAgent", "feature-rag-specialist"), ("SandboxAgent", "drift-specialist")])
        ref_schema = SCHEMAS["SandboxAgent"]["schema"]["properties"]["spec"]["properties"]["substrate"]["properties"]["workerPoolRef"]
        self.assertNotIn("namespace", ref_schema["properties"])   # same-namespace pool only
        for doc in documents:
            self.assertEqual(doc["metadata"]["namespace"], "ewallet-sandbox-staging")
            self.assertEqual(doc["metadata"]["labels"][sa.EXECUTION_LABEL], EXECUTION)
            if doc["kind"] in ("WorkerPool", "SandboxAgent"):
                jsonschema.validate(doc["spec"], SCHEMAS[doc["kind"]]["schema"]["properties"]["spec"])
        pool = documents[0]["spec"]
        self.assertEqual((pool["replicas"], pool["sandboxClass"], "scaleSelector" in pool), (1, "gvisor", False))

    def test_fleet_keeps_the_production_actor_contract(self):
        from scripts.experiments.render_sandbox import render
        import yaml

        production = {d["metadata"]["name"]: d for d in yaml.safe_load_all(render("fleet", G3, "agi-20261118T000000Z", "rel-1"))
                      if d and d["kind"] == "SandboxAgent"}
        pool = next(d for d in yaml.safe_load_all((ROOT / "infra/kubernetes/agent-sandbox/namespace.yaml").read_text())
                    if d and d["kind"] == "WorkerPool")
        staging = {d["metadata"]["name"]: d for d in st.render_fleet(CONTRACT, G3, SHA, EXECUTION)}
        self.assertEqual(staging["staging-agents"]["spec"]["ateomImage"], pool["spec"]["ateomImage"])   # patched proxy
        for name, prod in production.items():
            stg = staging[name]
            p, s = prod["spec"]["byo"]["deployment"], stg["spec"]["byo"]["deployment"]
            self.assertEqual({k: v for k, v in p.items() if k != "env"}, {k: v for k, v in s.items() if k != "env"})
            p_env = {e["name"]: e for e in p["env"] if not e["name"].startswith(("OTEL_EXPORTER", "EWALLET_OTEL"))}
            s_env = {e["name"]: e for e in s["env"]}
            self.assertEqual(s_env["EWALLET_GATEWAY_NAMESPACE"]["value"], "ewallet-agents-staging")
            self.assertEqual(s_env["EWALLET_RUNTIME_NAMESPACE"]["value"], "ewallet-sandbox-staging")
            for key in set(p_env) - {"OTEL_TRACING_ENABLED", "EWALLET_MCP_SERVICE_TOKEN"}:
                self.assertEqual(p_env[key], s_env[key], key)   # port 80, PATH/PYTHONPATH, proxy unchanged
            self.assertEqual(s_env["EWALLET_MCP_SERVICE_TOKEN"]["valueFrom"]["secretKeyRef"]["name"],
                             CONTRACT["agents"][name]["credential_secret"])
            self.assertEqual(stg["spec"]["substrate"]["workerPoolRef"]["name"], "staging-agents")
            self.assertTrue(stg["spec"]["substrate"]["snapshotsConfig"]["location"].startswith(
                f"gs://ate-snapshots/ewallet-sandbox-staging/{EXECUTION}/"))
            text = json.dumps(stg)
            for production_ref in (".ewallet-agents.", "ewallet-sandbox/", "business-agents", "otel-collector"):
                self.assertNotIn(production_ref, text)


class FakeKube:
    """Objects keyed by (kind, namespace, name); SandboxAgents become Ready with a golden actor when created."""

    def __init__(self, missing=(), stale=()):
        self.objects: dict[tuple, dict] = {}
        self.missing, self.mutations, self.uid = set(missing), [], 0
        for item in stale:
            self.objects[item] = {"metadata": {"uid": "old", "labels": {}}}
        data = {"drift-baselines-staging": {"baselines.json": "{}"},
                "drift-window-staging": {"window-manifest.json": json.dumps({"schema_version": "ewallet-drift-window/v1", "dataset_revision": "staging-v1"})}}
        for name, content in data.items():
            self.objects[("ConfigMap", st.AGENTS_NAMESPACE, name)] = {"metadata": {"name": name}, "data": content}
        for dependency in CONTRACT["dependencies"]["deployments"]:   # operator-applied, Ready
            self.objects[("Deployment", st.AGENTS_NAMESPACE, dependency["name"])] = {
                "metadata": {"name": dependency["name"]}, "status": {"readyReplicas": 1},
                "spec": {"template": {"spec": {"containers": [{"image": dependency["image"]}]}}}}

    def available(self, group_version, plural):
        return True

    def exists(self, kind, namespace, name):
        return name not in self.missing

    def get(self, kind, namespace, name):
        return copy.deepcopy(self.objects.get((kind, namespace, name)))

    def list(self, kind, namespace):
        return [copy.deepcopy(v) for (k, ns, _), v in self.objects.items() if k == kind and ns == namespace]

    def create(self, document):
        self.uid += 1
        doc = copy.deepcopy(document)
        meta = doc["metadata"]
        meta.update(uid=f"uid-{self.uid}", generation=1)
        if doc["kind"] == "WorkerPool":
            doc["status"] = {"replicas": doc["spec"]["replicas"]}
        if doc["kind"] == "SandboxAgent":
            doc["status"] = {"observedGeneration": 1, "conditions": [{"type": t, "status": "True", "observedGeneration": 1}
                                                                       for t in ("Accepted", "Ready")]}
            deployment = doc["spec"]["byo"]["deployment"]
            self.objects[("ActorTemplate", meta["namespace"], f"{meta['name']}-actor")] = {
                "metadata": {"name": f"{meta['name']}-actor", "ownerReferences": [{"kind": "SandboxAgent", "uid": meta["uid"], "controller": True}]},
                "spec": {"sandboxClass": "gvisor", "workerSelector": {"matchLabels": {"kagent.dev/worker-pool": "staging-agents"}},
                         "containers": [{"image": deployment["image"], "command": [deployment["cmd"]], "args": deployment["args"]}]},
                "status": {"conditions": [{"type": "Ready", "status": "True"}], "goldenActorID": "golden-1"}}
        self.objects[(doc["kind"], meta["namespace"], meta["name"])] = doc
        self.mutations.append(("create", doc["kind"], meta["name"]))
        return doc

    def delete_uid(self, kind, namespace, name, uid):
        assert self.objects[(kind, namespace, name)]["metadata"]["uid"] == uid
        del self.objects[(kind, namespace, name)]
        for key in [k for k, v in self.objects.items() if k[0] == "ActorTemplate"
                    and any(o.get("uid") == uid for o in v["metadata"].get("ownerReferences", []))]:
            del self.objects[key]
        self.mutations.append(("delete", kind, name))


class FakeHelm:
    def __init__(self, kube, *, existing=False, manifest=None):
        self.kube, self.calls, self.existing = kube, [], existing
        self.manifest = manifest or f"kind: ConfigMap\nmetadata: {{name: x, namespace: {st.AGENTS_NAMESPACE}}}\n"

    def __call__(self, args):
        self.calls.append(args[0])
        out = ""
        code = 0
        if args[0] == "status":
            code = 0 if self.existing else 1
        elif args[0] == "template":
            out = self.manifest
        elif args[0] == "install":   # kagent then creates the Coordinator Deployment from the Agent CR
            self.kube.objects[("Deployment", st.AGENTS_NAMESPACE, "fraud-coordinator")] = {"status": {"readyReplicas": 1}}
        return subprocess.CompletedProcess(args, code, out, "")


@contextlib.contextmanager
def forward(target):
    yield "http://127.0.0.1:18083/"


class ExecutorTests(unittest.TestCase):
    def execute(self, kube, helm, *, verify_failure=None, unit=UNIT, plan=PLAN):
        def fake_verify(contract, plan, image, forward, post, status, evidence, report, clock, sleep):
            report.e2e.append({"check": "coordinator-fraud", "verdict": verify_failure or "PASS"})
            if verify_failure:
                report.fail("E2E_FAILED", [verify_failure])

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(st, "verify", side_effect=fake_verify):
            return st.run(kube, unit, plan, MANIFEST, helm=helm, forward=forward, execution_id=EXECUTION, contract=CONTRACT,
                          evidence=Path(tmp), clock=lambda: 0.0, sleep=lambda _: None)

    def test_happy_path_order_and_complete_cleanup(self):
        kube = FakeKube()
        helm = FakeHelm(kube)
        report = self.execute(kube, helm)
        self.assertEqual(report.status, "PASS", report.problems)
        self.assertEqual(report.steps, ["preflight", "helm-template", "helm-install", "fleet-created", "ready", "verified", "cleanup"])
        self.assertEqual(helm.calls, ["status", "template", "install", "uninstall"])
        created = [m for m in kube.mutations if m[0] == "create"]
        deleted = [m for m in kube.mutations if m[0] == "delete"]
        self.assertEqual([m[1] for m in created], ["WorkerPool", "ServiceAccount", "ServiceAccount", "SandboxAgent", "SandboxAgent"])
        self.assertEqual([m[1:] for m in deleted], [m[1:] for m in reversed(created)])   # SandboxAgents before the pool
        self.assertEqual(report.verification, ["AGENT_E2E_VERIFIED", "MANAGED_PROMPT_RUNTIME_NOT_YET_VERIFIED"])
        self.assertFalse([key for key in kube.objects if key[1] == st.SANDBOX_NAMESPACE])

    def test_failed_e2e_still_cleans_up(self):
        kube = FakeKube()
        helm = FakeHelm(kube)
        report = self.execute(kube, helm, verify_failure="KNOWN_ANSWER_MISMATCH")
        self.assertEqual((report.status, report.failure_code), ("FAIL", "E2E_FAILED"))
        self.assertIn("uninstall", helm.calls)
        self.assertFalse([key for key in kube.objects if key[1] == st.SANDBOX_NAMESPACE])
        self.assertEqual(report.verification, [])

    def test_refusals_happen_before_any_mutation(self):
        cases = {
            "MISSING_PREREQUISITE": (FakeKube(missing={"feast-online-redis-staging-fraud"}), {}),
            "STALE_STAGING": (FakeKube(stale={("SandboxAgent", st.SANDBOX_NAMESPACE, "drift-specialist")}), {}),
            "WRONG_TARGET": (FakeKube(), {"unit": {**UNIT, "target_namespace": "ewallet-agents"}}),
            "IDENTITY_MISMATCH": (FakeKube(), {"unit": {**UNIT, "images": {**UNIT["images"], "mcp-tools": MCP.replace("c" * 64, "e" * 64)}}}),
        }
        for code, (kube, overrides) in cases.items():
            with self.subTest(code):
                helm = FakeHelm(kube)
                report = self.execute(kube, helm, **overrides)
                self.assertEqual(report.failure_code, code, report.problems)
                self.assertEqual(kube.mutations, [])
                self.assertNotIn("install", helm.calls)
        for problem in ("missing", "image", "not-ready"):   # the staging data dependencies
            with self.subTest(problem):
                kube = FakeKube()
                key = ("Deployment", st.AGENTS_NAMESPACE, "fraud-detector-predictor")
                if problem == "missing":
                    del kube.objects[key]
                elif problem == "image":
                    kube.objects[key]["spec"]["template"]["spec"]["containers"][0]["image"] = "fraud-detector-predictor:latest"
                else:
                    kube.objects[key]["status"] = {}
                report = self.execute(kube, FakeHelm(kube))
                self.assertEqual(report.failure_code, "MISSING_PREREQUISITE")
                self.assertTrue(any("fraud-detector-predictor" in p for p in report.problems), report.problems)
                self.assertEqual(kube.mutations, [])
        kube = FakeKube()
        helm = FakeHelm(kube, existing=True)   # an earlier release is never adopted
        self.assertEqual(self.execute(kube, helm).failure_code, "STALE_STAGING")
        self.assertEqual(helm.calls, ["status"])

    def test_render_outside_the_staging_namespace_is_refused(self):
        kube = FakeKube()
        helm = FakeHelm(kube, manifest="kind: NetworkPolicy\nmetadata: {name: x, namespace: feature-store}\n")
        report = self.execute(kube, helm)
        self.assertEqual(report.failure_code, "RENDER_UNSAFE")
        self.assertEqual((helm.calls, kube.mutations), (["status", "template"], []))

    def test_cleanup_never_deletes_an_object_it_cannot_prove_is_its_own(self):
        kube = FakeKube()
        helm = FakeHelm(kube)

        def relabel(contract, plan, image, forward, post, status, evidence, report, clock, sleep):
            kube.objects[("WorkerPool", st.SANDBOX_NAMESPACE, "staging-agents")]["metadata"]["labels"][sa.EXECUTION_LABEL] = "someone-else"

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(st, "verify", side_effect=relabel):
            report = st.run(kube, UNIT, PLAN, MANIFEST, helm=helm, forward=forward, execution_id=EXECUTION,
                            contract=CONTRACT, evidence=Path(tmp), clock=lambda: 0.0, sleep=lambda _: None)
        self.assertEqual(report.status, "CLEANUP_INCOMPLETE")
        self.assertIn(("WorkerPool", st.SANDBOX_NAMESPACE, "staging-agents"), kube.objects)
        self.assertIn("NOT_OURS_LEFT_UNTOUCHED", {item["state"] for item in report.cleanup})

    def test_helm_keeps_its_release_record_in_configmaps(self):
        with mock.patch.object(st.subprocess, "run") as run:
            st._helm_runner("in-cluster")(["status", st.RELEASE])
        self.assertEqual(run.call_args.kwargs["env"]["HELM_DRIVER"], "configmap")

    def test_helm_values_carry_only_release_identities(self):
        args = st.helm_install_args(CONTRACT, UNIT["images"], SHA, EXECUTION,
                                    {"baselines_sha256": "1" * 64, "window_sha256": "2" * 64, "dataset_revision": "staging-v1"})
        self.assertEqual(args[:4], ["llm-agent-staging", str(ROOT / "infra/helm/llm-agent-platform"), "--namespace", "ewallet-agents-staging"])
        sets = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--set-string")
        self.assertEqual(sets["agents.coordinator.byo.image"], G3)
        self.assertEqual(sets["mcpServices.identity.imageDigest"], "sha256:" + "c" * 64)
        self.assertEqual(sets["mcpServices.identity.configRevision"], f"staging-{EXECUTION}")


class VerifyWiringTests(unittest.TestCase):
    """verify() runs the existing checks against the staging targets only."""

    def test_checks_target_staging_and_failures_fail_the_release(self):
        from ops.llm_agent import replay_verdict
        from scripts.llm_agent import agi_probe

        targets, urls, probes = [], [], []

        def fake_specialist(**kwargs):
            targets.append((kwargs["contract"]["namespace"], kwargs["target"], kwargs["endpoint"]))
            return st.specialist_e2e.SpecialistVerdict(kwargs["agent"], kwargs["target"], "PASS", kwargs["request_id"],
                                                       "url", "env", SHA, G3, gate="PASS")

        def fake_task(post, url, body, headers, *, timeout, clock, sleep):
            urls.append(url)
            return {"result": {}}, 1.0

        def fake_route(args, opener=None):
            probes.append((args.kagent_host, args.control_path))
            return {"verdict": "PASS", "reasons": []}

        report = st.StagingReport("agent-staging", EXECUTION)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(st.specialist_e2e, "verify", side_effect=fake_specialist), \
                mock.patch.object(st.specialist_e2e, "run_task", side_effect=fake_task), \
                mock.patch.object(replay_verdict, "e2e", return_value=("COMPLETE", [], {})), \
                mock.patch.object(agi_probe, "a2a_route", side_effect=fake_route):
            st.verify(CONTRACT, PLAN, G3, forward, None, lambda url, body, headers: 401, Path(tmp), report,
                      lambda: 0.0, lambda _: None)
        self.assertEqual(report.failure_code, None, report.problems)
        self.assertEqual({t[0] for t in targets}, {"ewallet-sandbox-staging"})
        self.assertEqual(set(urls), {"http://127.0.0.1:18083/api/a2a/ewallet-agents-staging/fraud-coordinator/"})
        self.assertEqual(len(urls), 3)   # fraud, drift, combined
        self.assertEqual([p[0] for p in probes], ["payments-agent.ewallet-agents-staging", "fraud-coordinator.ewallet-agents"])
        self.assertTrue(all(p[1] == "/api/a2a-sandboxes/ewallet-sandbox-staging/drift-specialist/" for p in probes))

        report = st.StagingReport("agent-staging", EXECUTION)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(st.specialist_e2e, "verify", side_effect=fake_specialist), \
                mock.patch.object(st.specialist_e2e, "run_task", side_effect=fake_task), \
                mock.patch.object(replay_verdict, "e2e", return_value=("PARTIAL", ["drift missing"], {})), \
                mock.patch.object(agi_probe, "a2a_route", side_effect=fake_route):
            st.verify(CONTRACT, PLAN, G3, forward, None, lambda url, body, headers: 200, Path(tmp), report,
                      lambda: 0.0, lambda _: None)   # anonymous MCP accepted and a partial Coordinator answer
        self.assertEqual(report.failure_code, "E2E_FAILED")
        self.assertTrue(any("mcp-anonymous-refused" in p for p in report.problems))

    def test_coordinator_requests_come_from_the_specialist_fixtures(self):
        from ai_platform.llm_agent.business_request import typed_request_from_a2a

        for case in ("fraud", "drift", "combined"):
            with self.subTest(case):
                typed = typed_request_from_a2a(st._requests(CONTRACT, case, f"staging-{EXECUTION}-{case}"))
                self.assertEqual(typed.request_id, f"staging-{EXECUTION}-{case}")


if __name__ == "__main__":
    unittest.main()
