"""Agent integration: artifact-backed drift baselines and window against a local fixture.

Real components: ``ArtifactBaselineStore``, ``CsvWindowSampler``, the existing
``DriftDetectionTool`` PSI, and the production ``create_drift_app`` (real
service-token authentication, tool grants and MCP Streamable HTTP), served
in-process. Data: the synthetic fixture from ``ops/llm_agent/drift_artifacts.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient

from ai_platform.inference.errors import ConfigurationError
from ai_platform.llm_agent.drift_evidence import DriftFinding
from ai_platform.llm_agent.business_request import G3DriftRequest
from ai_platform.llm_agent.contracts import RequestContext
from ai_platform.llm_agent.tools.drift import DriftDetectionInput, DriftDetectionTool, population_stability_index
from ai_platform.llm_agent.tools.drift_sources import (
    ArtifactBaselineStore,
    CsvWindowSampler,
    drift_backends_from_env,
)
from ai_platform.llm_agent.tools.runtime import ToolRejectedError, ToolUnavailableError
from ai_platform.llm_agent.tools.service import create_drift_app
from ops.llm_agent import drift_artifacts
from tests.llm_agent.agi_support import (
    ACCOUNT_BASELINE,
    ACCOUNT_FEATURE,
    CONTROLLER_TOKEN,
    CURRENT_WINDOW,
    DRIFT_TOKEN,
    FEATURE_RAG_TOKEN,
    USER_BASELINE,
    USER_FEATURE,
    DriftFixture,
)

CONTEXT = RequestContext(request_id="agi-drift-1", traceparent="00-" + "1" * 32 + "-" + "2" * 16 + "-01",
                         actor_id="drift-specialist", timeout_ms=10_000, tool_call_budget=1)


def run(coro):
    return asyncio.run(coro)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


class ArtifactBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = DriftFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def backends(self, **overrides):
        return drift_backends_from_env({**self.fixture.drift_env, **overrides})

    def tool(self, **overrides) -> DriftDetectionTool:
        return DriftDetectionTool(*self.backends(**overrides))

    def call(self, tool: DriftDetectionTool, feature=ACCOUNT_FEATURE, baseline=ACCOUNT_BASELINE, window=CURRENT_WINDOW):
        arguments = DriftDetectionInput(feature_name=feature, baseline_id=baseline, current_window=window)
        return run(tool.run(arguments, CONTEXT)).result

    def test_psi_is_computed_from_versioned_baseline_and_window(self):
        result = self.call(self.tool())
        finding = DriftFinding.model_validate(result)
        self.assertEqual(finding.dataset_revisions.reference, "agi-fixture-synthetic-v1")
        self.assertEqual(finding.dataset_revisions.current, "agi-fixture-synthetic-v1")
        self.assertGreaterEqual(finding.current_count, finding.min_current_count)
        # The fixture doubles the account velocity in May: a real distribution shift.
        self.assertEqual(finding.drift_status, "drift")
        self.assertGreater(finding.psi, 0.25)
        self.assertEqual(result["baseline_source"]["reference_count"], 3086)
        self.assertRegex(result["current_source"]["sha256"], r"^[0-9a-f]{64}$")

    def test_psi_matches_an_independent_recomputation(self):
        baselines, sampler = self.backends()
        baseline = run(baselines.get(ACCOUNT_BASELINE, ACCOUNT_FEATURE))
        sample = run(sampler.sample(ACCOUNT_FEATURE, utc(CURRENT_WINDOW["start"]), utc(CURRENT_WINDOW["end"])))
        psi, _ = population_stability_index(baseline, sample.values)
        self.assertAlmostEqual(self.call(self.tool())["psi"], psi, places=12)
        self.assertEqual(len(sample.values), self.call(self.tool())["current_count"])

    def test_missing_thresholds_give_unclassified_and_nulls_are_counted(self):
        result = self.call(self.tool(), feature=USER_FEATURE, baseline=USER_BASELINE)
        self.assertEqual((result["drift_status"], result["thresholds"]), ("unclassified", None))
        self.assertGreater(result["current_source"]["excluded_null_count"], 0)

    def test_insufficient_samples_return_no_psi(self):
        window = {"start": "2026-05-01T00:00:00+00:00", "end": "2026-05-01T06:00:00+00:00"}
        result = self.call(self.tool(), window=window)
        self.assertEqual((result["drift_status"], result["psi"], result["bins"]), ("insufficient_data", None, []))
        DriftFinding.model_validate(result)

    def test_rejections(self):
        tool = self.tool()
        cases = {
            "missing baseline": ({"baseline": "bl-does-not-exist"}, "unknown_baseline"),
            "baseline of another feature": ({"feature": USER_FEATURE}, "unknown_baseline"),
            "overlapping window": ({"window": {"start": "2026-04-20T00:00:00+00:00",
                                                "end": "2026-05-10T00:00:00+00:00"}}, "window_overlaps_reference"),
            "outside coverage": ({"window": {"start": "2026-05-20T00:00:00+00:00",
                                              "end": "2026-06-05T00:00:00+00:00"}}, "window_outside_coverage"),
        }
        for label, (changes, code) in cases.items():
            with self.subTest(label), self.assertRaises(ToolRejectedError) as caught:
                self.call(tool, **changes)
            self.assertEqual(caught.exception.code, code)

    def test_unsupported_feature_is_rejected_by_the_sampler(self):
        _, sampler = self.backends()
        with self.assertRaises(ToolRejectedError) as caught:
            run(sampler.sample("device_behavior__device_tx_count_1h", utc(CURRENT_WINDOW["start"]),
                               utc(CURRENT_WINDOW["end"])))
        self.assertEqual(caught.exception.code, "unsupported_feature")

    def test_invalid_window_is_rejected_by_the_tool_contract(self):
        with self.assertRaises(ValueError):
            DriftDetectionInput(feature_name=ACCOUNT_FEATURE, baseline_id=ACCOUNT_BASELINE,
                                current_window={"start": CURRENT_WINDOW["end"], "end": CURRENT_WINDOW["start"]})

    def test_wrong_revision_digest_or_synthetic_policy_fails_closed(self):
        cases = {
            "dataset revision": ({"EWALLET_DRIFT_DATASET_REVISION": "feast-r5gb-20261111-214544"},
                                 "drift_dataset_revision_mismatch"),
            "baseline digest": ({"EWALLET_DRIFT_BASELINES_SHA256": "0" * 64}, "drift_baseline_checksum_mismatch"),
            "window digest": ({"EWALLET_DRIFT_WINDOW_MANIFEST_SHA256": "0" * 64}, "drift_window_checksum_mismatch"),
            "synthetic refused": ({"EWALLET_DRIFT_ALLOW_SYNTHETIC": "false"}, "drift_synthetic_artifact_refused"),
        }
        for label, (overrides, code) in cases.items():
            with self.subTest(label):
                tool = self.tool(**overrides)
                with self.assertRaises(ToolUnavailableError) as caught:
                    run(tool.ready())
                self.assertEqual(caught.exception.code, code)

    def test_tampered_observation_file_is_refused(self):
        copy = self.fixture.root / "tampered"
        shutil.copytree(self.fixture.window_manifest.parent, copy)
        target = copy / "account_behavior.csv.gz"
        target.write_bytes(target.read_bytes()[:-8] + b"\x00" * 8)
        sampler = CsvWindowSampler(copy / "window-manifest.json",
                                   sha256=self.fixture.drift_env["EWALLET_DRIFT_WINDOW_MANIFEST_SHA256"],
                                   dataset_revision="agi-fixture-synthetic-v1", allow_synthetic=True)
        with self.assertRaises(ToolUnavailableError) as caught:
            run(sampler.ready())
        self.assertEqual(caught.exception.code, "drift_window_checksum_mismatch")

    def test_incomplete_configuration_stops_startup(self):
        for missing in ("EWALLET_DRIFT_BASELINES_SHA256", "EWALLET_DRIFT_WINDOW_MANIFEST", "EWALLET_DRIFT_DATASET_REVISION"):
            with self.subTest(missing), self.assertRaises(ConfigurationError):
                drift_backends_from_env({**self.fixture.drift_env, missing: ""})
        with self.assertRaises(ConfigurationError):
            drift_backends_from_env({**self.fixture.drift_env, "EWALLET_DRIFT_SOURCE": "redis"})
        with self.assertRaises(ConfigurationError):
            drift_backends_from_env({**self.fixture.drift_env, "EWALLET_DRIFT_DATASET_REVISION": "latest version"})

    def test_default_wiring_stays_explicitly_unavailable(self):
        tool = DriftDetectionTool(*drift_backends_from_env({}))
        with self.assertRaises(ToolUnavailableError) as caught:
            run(tool.ready())
        self.assertEqual(caught.exception.code, "drift_baseline_unavailable")

    def test_duplicate_baseline_ids_are_refused(self):
        document = json.loads(self.fixture.baselines.read_text())
        document["baselines"].append(document["baselines"][0])
        path = self.fixture.root / "duplicate.json"
        path.write_text(json.dumps(document))
        import hashlib
        store = ArtifactBaselineStore(path, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                      dataset_revision="agi-fixture-synthetic-v1", allow_synthetic=True)
        with self.assertRaises(ToolUnavailableError):
            run(store.ready())


class ArtifactToolTests(unittest.TestCase):
    def test_quantile_bins_collapse_ties_and_proportions_sum_to_one(self):
        values = [0.0] * 50 + [1.0] * 30 + [2.0] * 20
        cuts = drift_artifacts.quantile_cut_points(values, 10)
        self.assertEqual(cuts, (0.0, 1.0))
        self.assertAlmostEqual(sum(drift_artifacts.reference_proportions(values, cuts)), 1.0)
        with self.assertRaises(drift_artifacts.ArtifactError):
            drift_artifacts.quantile_cut_points([3.0] * 10, 5)

    def test_artifacts_are_create_only_and_reproducible(self):
        fixture = DriftFixture()
        try:
            with self.assertRaises(drift_artifacts.ArtifactError):
                drift_artifacts.write_fixture(fixture.root / "artifacts")
            again = DriftFixture()
            try:
                for name in ("EWALLET_DRIFT_BASELINES_SHA256", "EWALLET_DRIFT_WINDOW_MANIFEST_SHA256"):
                    self.assertEqual(fixture.drift_env[name], again.drift_env[name])
            finally:
                again.close()
        finally:
            fixture.close()


@unittest.skipUnless(importlib.util.find_spec("pyarrow") is not None, "pyarrow unavailable (uv --extra feast)")
class SnapshotExtractionTests(unittest.TestCase):
    """baseline/window read a Feast Offline snapshot laid out like export_gold_to_parquet.py."""

    def setUp(self):
        import hashlib

        import pyarrow as pa
        import pyarrow.parquet as pq

        self.directory = tempfile.TemporaryDirectory(prefix="agi-snapshot-")
        self.root = Path(self.directory.name)
        snapshot = self.root / "snapshot"
        (snapshot / "_control").mkdir(parents=True)
        (snapshot / "feat_account_behavior").mkdir()
        manifest = json.dumps({"lineage": {"snapshot_id": "feast-test-20261115"}, "tables": {}}).encode()
        (snapshot / "_control/export_manifest.json").write_bytes(manifest)
        self.manifest_sha = hashlib.sha256(manifest).hexdigest()
        start = datetime(2026, 4, 1)
        rows = 4_000
        stamps = [start + timedelta(minutes=15 * index) for index in range(rows)]  # naive UTC, as exported
        table = pa.table({
            "account_id": [f"acct-{index % 97}" for index in range(rows)],
            "event_timestamp": pa.array(stamps, type=pa.timestamp("us")),
            "account_tx_count_1h": [index % 5 + (2 if stamp.month == 5 else 0) for index, stamp in enumerate(stamps)],
        })
        pq.write_table(table, snapshot / "feat_account_behavior/part-0.parquet")
        self.snapshot = snapshot

    def tearDown(self):
        self.directory.cleanup()

    def common(self):
        return ["--snapshot", str(self.snapshot), "--expect-manifest-sha256", self.manifest_sha,
                "--feature", ACCOUNT_FEATURE]

    def test_baseline_and_window_from_a_snapshot_feed_the_server_loaders(self):
        baselines = self.root / "out/baselines.json"
        self.assertEqual(drift_artifacts.main(["baseline", *self.common(), "--reference-start", "2026-04-01T00:00:00Z",
                                               "--reference-end", "2026-05-01T00:00:00Z", "--baseline-id-prefix",
                                               "bl-test", "--thresholds", "0.1,0.25", "--out", str(baselines)]), 0)
        window = self.root / "out/window"
        self.assertEqual(drift_artifacts.main(["window", *self.common(), "--start", "2026-05-01T00:00:00Z",
                                               "--end", "2026-05-12T00:00:00Z", "--max-rows", "500",
                                               "--out-dir", str(window)]), 0)
        report = drift_artifacts.describe(baselines, window / "window-manifest.json")
        self.assertFalse(report["synthetic"])
        self.assertEqual(report["environment"]["EWALLET_DRIFT_DATASET_REVISION"], "feast-test-20261115")
        self.assertEqual(report["window"]["files"][0]["sampling"]["method"], "systematic")
        self.assertLessEqual(report["window"]["files"][0]["rows"], 500)
        tool = DriftDetectionTool(*drift_backends_from_env({**report["environment"]}))
        result = run(tool.run(DriftDetectionInput(feature_name=ACCOUNT_FEATURE, baseline_id="bl-test.account_tx_count_1h",
                                                  current_window={"start": "2026-05-01T00:00:00Z",
                                                                  "end": "2026-05-12T00:00:00Z"}), CONTEXT)).result
        self.assertEqual(result["drift_status"], "drift")

    def test_snapshot_with_another_manifest_digest_is_refused(self):
        code = drift_artifacts.main(["baseline", "--snapshot", str(self.snapshot), "--expect-manifest-sha256", "0" * 64,
                                     "--feature", ACCOUNT_FEATURE, "--reference-start", "2026-04-01T00:00:00Z",
                                     "--reference-end", "2026-05-01T00:00:00Z", "--baseline-id-prefix", "bl-test",
                                     "--out", str(self.root / "x.json")])
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "x.json").exists())


class DriftServiceTests(unittest.TestCase):
    """The production drift-mcp app over MCP Streamable HTTP, served in-process."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = DriftFixture()
        cls.client = TestClient(create_drift_app(cls.fixture.server_env()))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.fixture.close()

    def rpc(self, token: str, method: str, params: dict | None = None, request_id: str = "agi-drift-svc-1"):
        response = self.client.post("/mcp", headers={
            "X-Ewallet-MCP-Service-Token": token, "accept": "application/json, text/event-stream",
            "content-type": "application/json", "x-request-id": request_id,
        }, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def arguments(self) -> dict:
        return G3DriftRequest(request_id="agi-drift-svc-1", feature_name=ACCOUNT_FEATURE,
                              baseline_id=ACCOUNT_BASELINE, current_window=CURRENT_WINDOW).tool_arguments()

    def test_authorized_specialist_discovers_and_calls_drift_detection(self):
        tools = self.rpc(DRIFT_TOKEN, "tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["drift_detection"])
        result = self.rpc(DRIFT_TOKEN, "tools/call", {"name": "drift_detection", "arguments": self.arguments()})["result"]
        self.assertFalse(result["isError"])
        envelope = result["structuredContent"]
        self.assertEqual((envelope["request_id"], envelope["status"]), ("agi-drift-svc-1", "ok"))
        self.assertEqual(envelope["versions"]["feature_revision"], "agi-fixture-synthetic-v1")
        DriftFinding.model_validate(envelope["result"])

    def test_unauthorized_principals_are_denied(self):
        for token in (FEATURE_RAG_TOKEN, CONTROLLER_TOKEN):
            with self.subTest(token=token[:12]):
                denied = self.rpc(token, "tools/call", {"name": "drift_detection", "arguments": self.arguments()})
                self.assertEqual(denied["error"]["message"], "Unknown tool")
        response = self.client.post("/mcp", headers={"accept": "application/json", "content-type": "application/json"},
                                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(response.status_code, 401)

    def test_capabilities_report_non_secret_data_identity(self):
        response = self.client.get("/v1/capabilities", headers={"X-Ewallet-MCP-Service-Token": DRIFT_TOKEN})
        body = response.json()
        self.assertEqual(body["tools"]["drift_detection"]["status"], "ready")
        self.assertEqual(body["configuration"]["dataset_revision"], "agi-fixture-synthetic-v1")
        self.assertTrue(body["configuration"]["synthetic_allowed"])
        self.assertNotIn(DRIFT_TOKEN, response.text)

    def test_missing_source_data_is_an_explicit_unavailable_result(self):
        broken = DriftFixture()
        try:
            (broken.window_manifest.parent / "account_behavior.csv.gz").unlink()
            with TestClient(create_drift_app(broken.server_env())) as client:
                response = client.post("/mcp", headers={
                    "X-Ewallet-MCP-Service-Token": DRIFT_TOKEN, "accept": "application/json",
                    "content-type": "application/json"}, json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "drift_detection", "arguments": self.arguments()}})
            result = response.json()["result"]
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["status"], "unavailable")
            self.assertEqual(result["structuredContent"]["result"]["error"]["code"], "drift_window_unavailable")
        finally:
            broken.close()


if __name__ == "__main__":
    unittest.main()
