"""Agent integration: evidence helpers (agi_probe.py, agi_inputs.py).

``agi_probe mcp`` runs against the real ``create_drift_app`` served in-process
(``httpx2.ASGITransport``); no cluster, port-forward or Secret is touched.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from ai_platform.llm_agent.business_request import typed_request_from_a2a
from ops.llm_agent import agi_inputs
from tests.llm_agent.agi_support import (
    ACCOUNT_BASELINE,
    ACCOUNT_FEATURE,
    CURRENT_WINDOW,
    DRIFT_TOKEN,
    FEATURE_RAG_TOKEN,
    DriftFixture,
)
from tests.llm_agent.test_agi_contracts import TRANSACTION

ROOT = Path(__file__).resolve().parents[2]
SDK = all(importlib.util.find_spec(name) is not None for name in ("mcp", "httpx2"))
spec = importlib.util.spec_from_file_location("agi_probe", ROOT / "scripts/llm_agent/agi_probe.py")
agi_probe = importlib.util.module_from_spec(spec)
sys.modules["agi_probe"] = agi_probe
spec.loader.exec_module(agi_probe)


class ProbeRequestTests(unittest.TestCase):
    def test_every_kind_writes_a_canonical_typed_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            transaction = Path(tmp) / "transaction.json"
            transaction.write_text(json.dumps(TRANSACTION))
            drift = ["--feature", ACCOUNT_FEATURE, "--baseline", ACCOUNT_BASELINE,
                     "--window-start", CURRENT_WINDOW["start"], "--window-end", CURRENT_WINDOW["end"]]
            cases = {"fraud": ["--transaction", str(transaction)], "drift": drift,
                     "combined": ["--transaction", str(transaction), *drift]}
            for kind, extra in cases.items():
                with self.subTest(kind):
                    out = Path(tmp) / f"{kind}.json"
                    self.assertEqual(agi_probe.main(["request", kind, "--request-id", f"agi-t-{kind}", *extra,
                                                     "--out", str(out)]), 0)
                    request = typed_request_from_a2a(json.loads(out.read_text()))
                    self.assertEqual(request.request_id, f"agi-t-{kind}")
                    with self.assertRaises(SystemExit):  # create-only
                        agi_probe.main(["request", kind, "--request-id", "x", *extra, "--out", str(out)])

    def test_incomplete_drift_request_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            agi_probe.main(["request", "drift", "--request-id", "x", "--feature", ACCOUNT_FEATURE,
                            "--out", str(Path(tmp) / "x.json")])


@unittest.skipUnless(SDK, "official MCP SDK unavailable")
class ProbeMcpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import httpx2

        from ai_platform.llm_agent.tools.service import create_drift_app

        cls.fixture = DriftFixture()
        cls.transport = httpx2.ASGITransport(app=create_drift_app(cls.fixture.server_env()))

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def probe(self, token, arguments):
        return asyncio.run(agi_probe.call_tool("http://testserver/mcp", token, "drift_detection", arguments, 10,
                                               transport=self.transport))

    def arguments(self, **changes):
        value = {"feature_name": ACCOUNT_FEATURE, "baseline_id": ACCOUNT_BASELINE, "current_window": CURRENT_WINDOW}
        value.update(changes)
        return value

    def test_outcomes_match_the_real_server(self):
        self.assertEqual(self.probe(DRIFT_TOKEN, self.arguments())["outcome"], "ok")
        self.assertEqual(self.probe(FEATURE_RAG_TOKEN, self.arguments())["outcome"], "denied")
        self.assertEqual(self.probe(DRIFT_TOKEN, self.arguments(baseline_id="bl-missing"))["outcome"], "rejected")
        self.assertEqual(self.probe(DRIFT_TOKEN, self.arguments(baseline_id="latest"))["outcome"], "rejected")


class InputsTests(unittest.TestCase):
    def test_database_is_made_explicit_in_both_connection_forms(self):
        self.assertEqual(agi_inputs.with_database("redis.fs.svc:6379,password=p,db=0", 3),
                         "redis.fs.svc:6379,password=p,db=3")
        self.assertEqual(agi_inputs.with_database("redis.fs.svc:6379,password=p", 0), "redis.fs.svc:6379,password=p,db=0")
        self.assertEqual(agi_inputs.with_database("redis://:p@redis.fs.svc:6379", 3), "redis://:p@redis.fs.svc:6379/3")
        for bad in ("", "not a connection"):
            with self.subTest(bad), self.assertRaises(agi_inputs.InputError):
                agi_inputs.with_database(bad, 3)
        with self.assertRaises(agi_inputs.InputError):
            agi_inputs.with_database("redis.fs.svc:6379", 16)

    def test_secret_plan_never_contains_the_connection(self):
        document = agi_inputs.derived_secret("feast-online-redis-agi-rag", "ewallet-mcp", "connection_string",
                                             "redis.fs.svc:6379,password=s3cret,db=3", "feast-online-redis", 3)
        self.assertEqual(base64.b64decode(document["data"]["connection_string"]).decode(),
                         "redis.fs.svc:6379,password=s3cret,db=3")
        self.assertNotIn("s3cret", json.dumps(agi_inputs._plan(document)))
        self.assertTrue(document["immutable"])

    def test_cli_refuses_db0_and_unapproved_copies_before_reading_a_secret(self):
        base = ["redis-secret", "--context", "ctx", "--source", "feast-online-redis", "--name",
                "feast-online-redis-agi-fraud"]
        for extra in (["--db", "0", "--approve-sensitive-copy"], ["--db", "2"]):
            with self.subTest(extra), self.assertRaises(SystemExit):
                agi_inputs.main([*base, *extra])

    def test_synthetic_drift_artifacts_are_refused_for_the_cluster(self):
        fixture = DriftFixture()
        try:
            with self.assertRaises(agi_inputs.InputError):
                agi_inputs.drift_configmaps(fixture.baselines, fixture.window_manifest, "ewallet-mcp", "agi")
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
