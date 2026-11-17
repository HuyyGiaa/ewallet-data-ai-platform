"""Release dispatch: re-verification, typed executors, ordering, fail-first and locks (offline, fake runner)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.ci.release_dispatch import DispatchError, commands_for, dispatch_rehearsal


ROOT = Path(__file__).resolve().parents[2]
SHA = "0123456789abcdef0123456789abcdef01234567"


def ref(name: str) -> str:
    return f"registry.example/ewallet/{name}@sha256:" + "a" * 64


def unit(name: str, kind: str, images: dict[str, str], lock: str = "cicd-agent-platform", contract: str = "c.json"):
    return {"name": name, "kind": kind, "images": images, "lock": lock, "contract": contract}


PLAN = {"git_sha": SHA, "release_branch": "release/coordinator", "kube_context": "in-cluster",
        "units": [unit("mcp-servers", "bounded-rehearsal", {"mcp-tools": ref("mcp-tools")}),
                  unit("agent-fleet", "bounded-rehearsal", {"g3-byo-coordinator": ref("g3")}),
                  unit("coordinator", "bounded-rehearsal", {"g3-byo-coordinator": ref("g3")})]}


class FakeLocks:
    def __init__(self) -> None:
        self.held: list[list[str]] = []

    def hold(self, names):  # type: ignore[no-untyped-def]
        from contextlib import contextmanager

        @contextmanager
        def held():
            self.held.append(list(names))
            yield names

        return held()


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.evidence = Path(directory.name)

    def test_units_run_in_plan_order_under_their_locks(self) -> None:
        calls: list[list[str]] = []
        locks = FakeLocks()
        report = dispatch_rehearsal(PLAN, self.evidence, run=lambda argv, env: calls.append(list(argv)) or 0, locks=locks)
        self.assertEqual("PASS", report["verdict"])
        self.assertFalse(report["production_changed"])
        self.assertEqual([["cicd-agent-platform"]], locks.held)
        self.assertEqual(["mcp-servers", "agent-fleet", "coordinator"], [call[call.index("--unit") + 1] for call in calls])

    def test_the_first_failure_stops_the_release(self) -> None:
        calls: list[list[str]] = []

        def run(argv, env):  # type: ignore[no-untyped-def]
            calls.append(list(argv))
            return 1 if "mcp-servers" in argv else 0

        report = dispatch_rehearsal(PLAN, self.evidence, run=run, locks=FakeLocks())
        self.assertEqual("FAIL", report["verdict"])
        self.assertEqual(["agent-fleet", "coordinator"], report["not_run"])
        self.assertEqual(1, len(calls))

    def test_executors_come_from_the_kind_never_from_the_plan(self) -> None:
        rag = commands_for(unit("rag-pipeline", "rag-rehearsal", {"rag-ingestion": ref("rag")}), PLAN, self.evidence)
        self.assertEqual("scripts/ci/deploy/rag_pipeline.sh", rag[0][0])
        bounded = commands_for(PLAN["units"][0], PLAN, self.evidence)[0]
        self.assertIn("scripts.ci.bounded_cd", bounded)
        for kind in ("helm-image-promotion", "fleet-image-promotion", "kubectl-apply"):
            with self.subTest(kind), self.assertRaises(DispatchError):
                commands_for(unit("x", kind, {}), PLAN, self.evidence)

    def test_streaming_runs_the_image_smoke_before_the_campaign_with_both_images(self) -> None:
        streaming = unit("streaming-block-a", "streaming-verification",
                         {"flink-block-a": ref("flink"), "streaming-sinks": ref("sinks")},
                         lock="block-a-verification", contract="jenkins/cd/streaming.contract.json")
        smoke, campaign = commands_for(streaming, PLAN, self.evidence)
        self.assertIn(f"flink-block-a={ref('flink')}", smoke)
        self.assertNotIn(f"streaming-sinks={ref('sinks')}", smoke)
        self.assertIn("scripts.ci.streaming_verify", campaign)
        self.assertIn(f"streaming-sinks={ref('sinks')}", campaign)
        self.assertIn("--execute", campaign)
        # Never the plan's in-cluster Jenkins context.
        self.assertNotIn("in-cluster", campaign)

    def streaming_plan(self) -> dict:
        streaming = unit("streaming-block-a", "streaming-verification",
                         {"flink-block-a": ref("flink"), "streaming-sinks": ref("sinks")},
                         lock="block-a-verification", contract="jenkins/cd/streaming.contract.json")
        return {**PLAN, "release_branch": "release/streaming", "units": [streaming]}

    def test_streaming_without_an_identity_is_blocked_and_runs_nothing(self) -> None:
        from scripts.ci.streaming_identity import StreamingIdentityError

        calls: list[list[str]] = []

        def no_identity(workdir):  # type: ignore[no-untyped-def]
            raise StreamingIdentityError("BLOCKED: no streaming identity")

        report = dispatch_rehearsal(self.streaming_plan(), self.evidence, run=lambda a, e: calls.append(a) or 0,
                                    locks=FakeLocks(), streaming_identity=no_identity)
        self.assertEqual("FAIL", report["verdict"])
        self.assertEqual("BLOCKED", report["units"][0]["status"])
        self.assertEqual([], calls)
        self.assertIn("BLOCKED", (self.evidence / "streaming-block-a" / "blocked.txt").read_text())

    def test_streaming_runs_as_its_identity_and_the_token_file_is_removed(self) -> None:
        from scripts.ci.streaming_identity import StreamingIdentity

        seen: list[tuple[list[str], dict]] = []
        holder: dict[str, Path] = {}

        def identity(workdir):  # type: ignore[no-untyped-def]
            path = workdir / "streaming-kubeconfig"
            path.write_text("token: secret")
            holder["path"] = path
            return StreamingIdentity(path, "ewallet-cicd-streaming", "test")

        report = dispatch_rehearsal(self.streaming_plan(), self.evidence,
                                    run=lambda a, e: seen.append((list(a), dict(e))) or 0,
                                    locks=FakeLocks(), streaming_identity=identity)
        self.assertEqual("PASS", report["verdict"])
        campaign, env = seen[1]
        self.assertEqual("ewallet-cicd-streaming", campaign[campaign.index("--kube-context") + 1])
        self.assertEqual(str(holder["path"]), env["KUBECONFIG"])
        self.assertFalse(holder["path"].exists(), "the token-bearing kubeconfig is removed")
        self.assertFalse(str(holder["path"]).startswith(str(self.evidence)), "never inside archived evidence")

    def test_registry_gate_receives_the_registry_url(self) -> None:
        gate = commands_for(unit("prompt-registry-gate", "registry-gate", {}, contract=None), PLAN, self.evidence,
                            prompt_registry="http://registry.invalid")[0]
        self.assertEqual("http://registry.invalid", gate[gate.index("--registry") + 1])


class CliTests(unittest.TestCase):
    def test_a_tampered_or_stale_plan_is_refused_before_anything_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            plan.write_text(json.dumps({"schema_version": 1, "units": [{"handler": "scripts/llm_agent/deploy_atomic.sh"}]}))
            result = subprocess.run([sys.executable, "-m", "scripts.ci.release_dispatch", "--plan", str(plan), "--execute"],
                                    cwd=ROOT, capture_output=True, text=True, env={**os.environ, "K8S_API": "http://127.0.0.1:9"})
        self.assertEqual(2, result.returncode)
        self.assertIn("refused: release plan names the removed handler", result.stdout)

    def test_no_cluster_access_refuses_before_any_deployment(self) -> None:
        """Seen on the local controller preflight: without an in-cluster token the dispatcher must fail
        with a clear message, not a traceback, and run nothing."""
        from scripts.ci import release_dispatch
        from scripts.ci.k8s_api import KubeError

        def no_cluster(*args):  # type: ignore[no-untyped-def]
            raise KubeError("no in-cluster ServiceAccount token")

        with tempfile.TemporaryDirectory() as tmp:
            plan = {"stage": "rehearsal", "evidence_dir": tmp, "git_sha": SHA, "release_branch": "release/x",
                    "kube_context": "in-cluster", "units": []}
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps(plan))
            original = (release_dispatch.verify_release_plan, release_dispatch._locks, release_dispatch._git_head)
            release_dispatch.verify_release_plan = lambda *a, **k: None
            release_dispatch._locks = no_cluster
            release_dispatch._git_head = lambda: SHA
            try:
                code = release_dispatch.main(["--plan", str(path), "--execute"])
            finally:
                release_dispatch.verify_release_plan, release_dispatch._locks, release_dispatch._git_head = original
        self.assertEqual(2, code)

    def test_the_shell_entry_point_is_a_thin_locked_wrapper(self) -> None:
        text = (ROOT / "scripts/ci/deploy_release.sh").read_text()
        self.assertIn("uv run --locked --project infra/docker/llm-agent-tooling python -m scripts.ci.release_dispatch", text)
        self.assertNotIn("deploy_atomic", text)


if __name__ == "__main__":
    unittest.main()
