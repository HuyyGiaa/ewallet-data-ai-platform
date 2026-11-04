"""The registry security probe can never write to a Git-pinned prompt (incident F9)."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/llm_agent"))

import registry_security_probe as probe_module  # noqa: E402
from registry_security_probe import Probe, ProbeSafetyError, pinned_prompt_names, run  # noqa: E402

from ops.llm_agent import registry_catalog  # noqa: E402


class FakeRegistry:
    """In-memory registry that honours dryRun like agentregistry 0.4.0."""

    def __init__(self, persist_dry_run: bool = False) -> None:
        self.prompts = {"drift-specialist-system/v2": "pinned v2"}
        self.calls: list[tuple[str, str]] = []
        self.persist_dry_run = persist_dry_run

    def __call__(self, request, timeout):  # type: ignore[no-untyped-def]
        url, method = request.full_url, request.get_method()
        self.calls.append((method, url))
        body = b"{}"
        if "/v0/prompts/" in url:
            key = url.split("/v0/prompts/")[1]
            body = json.dumps({"spec": {"content": self.prompts[key]}}).encode()
        elif "/v0/apply" in url:
            doc = yaml.safe_load(request.data)
            if url.endswith("dryRun=false") or self.persist_dry_run:
                self.prompts[f"{doc['metadata']['name']}/{doc['metadata']['tag']}"] = doc["spec"]["content"]

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *args):  # type: ignore[no-untyped-def]
                return False
        return Response(body)


def prompt(name: str, tag: str) -> dict:
    return {"apiVersion": "ar.dev/v1alpha1", "kind": "Prompt", "metadata": {"name": name, "tag": tag},
            "spec": {"content": "x"}}


class RegistrySecurityProbeTests(unittest.TestCase):
    def test_default_probe_only_sends_dry_run_and_leaves_pinned_prompt_unchanged(self) -> None:
        registry = FakeRegistry()
        report = run(Probe("http://127.0.0.1:12121", opener=registry))
        self.assertEqual(report["verdict"], "PASS")
        writes = [url for method, url in registry.calls if method == "POST"]
        self.assertEqual(writes, ["http://127.0.0.1:12121/v0/apply?dryRun=true"])
        self.assertEqual(registry.prompts["drift-specialist-system/v2"], "pinned v2")

    def test_a_dry_run_that_persists_is_reported_as_an_incident(self) -> None:
        report = run(Probe("http://127.0.0.1:12121", opener=FakeRegistry(persist_dry_run=True)))
        self.assertTrue(report["verdict"].startswith("INCIDENT"))

    def test_live_write_without_the_global_flag_is_refused_before_any_request(self) -> None:
        registry = FakeRegistry()
        probe = Probe("http://127.0.0.1:12121", opener=registry)
        with self.assertRaises(ProbeSafetyError):
            probe.write(prompt("security-probe-x", "t1"), live=True)
        self.assertEqual(registry.calls, [])

    def test_live_write_must_match_the_declared_target(self) -> None:
        registry = FakeRegistry()
        probe = Probe("http://127.0.0.1:12121", allow_live_write=True, write_target="security-probe-x/t1",
                      opener=registry)
        with self.assertRaises(ProbeSafetyError):
            probe.write(prompt("security-probe-x", "t2"), live=True)
        probe.write(prompt("security-probe-x", "t1"), live=True)
        self.assertEqual(registry.calls[-1][1], "http://127.0.0.1:12121/v0/apply?dryRun=false")

    def test_pinned_prompts_and_non_probe_names_can_never_be_live_targets(self) -> None:
        pinned = pinned_prompt_names()
        self.assertIn("drift-specialist-system", pinned)
        for target in ("drift-specialist-system/v2", "fraud-coordinator-system/v9", "anything/v1"):
            with self.assertRaises(ProbeSafetyError):
                Probe("http://127.0.0.1:12121", allow_live_write=True, write_target=target)
        with self.assertRaises(ProbeSafetyError):
            Probe("http://127.0.0.1:12121", allow_live_write=True)
        with self.assertRaises(ProbeSafetyError):
            Probe("http://127.0.0.1:12121", write_target="security-probe-x/t1")

    def test_cli_requires_an_explicit_registry(self) -> None:
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", new_callable=io.StringIO):
            probe_module.main([])


class CatalogLiveWriteGuardTests(unittest.TestCase):
    def _main(self, argv: list[str]) -> tuple[int, mock.Mock]:
        with mock.patch.object(registry_catalog, "publish", return_value={"verdict": "PASS"}) as publish, \
                mock.patch.object(registry_catalog, "prompt_gate", return_value=(True, {})), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            try:
                code = registry_catalog.main(argv + ["--git-sha", "0" * 40])
            except SystemExit as exit_:
                code = int(exit_.code)
        return code, publish

    def test_publish_needs_an_explicit_registry(self) -> None:
        code, publish = self._main(["publish", "--dry-run"])
        self.assertEqual(code, 2)
        publish.assert_not_called()

    def test_live_publish_needs_matching_confirmation(self) -> None:
        for extra in ([], ["--confirm-live-write", "http://127.0.0.1:12122"]):
            code, publish = self._main(["publish", "--registry", "http://127.0.0.1:12121", *extra])
            self.assertEqual(code, 2)
            publish.assert_not_called()
        code, publish = self._main(["publish", "--registry", "http://127.0.0.1:12121",
                                    "--confirm-live-write", "http://127.0.0.1:12121"])
        self.assertEqual(code, 0)
        publish.assert_called_once()

    def test_dry_run_publish_needs_no_confirmation(self) -> None:
        code, publish = self._main(["publish", "--registry", "http://127.0.0.1:12121", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(publish.call_args.kwargs["dry_run"])

    def test_live_publish_is_blocked_by_a_failing_m3_pre_gate(self) -> None:
        with mock.patch.object(registry_catalog, "publish") as publish, \
                mock.patch.object(registry_catalog, "prompt_gate", return_value=(False, {"a/v1": "MISMATCH"})), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            code = registry_catalog.main(["publish", "--registry", "http://127.0.0.1:12121", "--confirm-live-write",
                                          "http://127.0.0.1:12121", "--git-sha", "0" * 40])
        self.assertEqual(code, 1)
        publish.assert_not_called()

    def test_failing_post_gate_fails_the_publish(self) -> None:
        with mock.patch.object(registry_catalog, "publish", return_value={"verdict": "PASS"}), \
                mock.patch.object(registry_catalog, "prompt_gate", side_effect=[(True, {}), (False, {"a/v1": "x"})]), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            code = registry_catalog.main(["publish", "--registry", "http://127.0.0.1:12121", "--confirm-live-write",
                                          "http://127.0.0.1:12121", "--git-sha", "0" * 40])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
