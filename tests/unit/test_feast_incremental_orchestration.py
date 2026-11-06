"""Stage ordering, retries and failure propagation for the Feast workflow.

The cluster and snapshot reader are local fakes. These tests prove orchestration
logic only; they are not evidence of a cloud materialization.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_platform.features.feast.materialization_plan import (
    ExistingJob,
    ReadbackError,
    WindowError,
)
from ai_platform.features.feast.schema import FEATURE_SPECS
from ops.mlops.feast_incremental import (
    ARTIFACTS,
    STAGES,
    StageError,
    context_from_environment,
    parse_operator_evidence,
    run_stage,
    validate_export_manifest,
)
from tests.unit.test_feast_incremental_plan import SOURCE_MAX, T00, T22, T23, parity


ENVIRONMENT = {
    "C1_DATA_BUCKET": "bucket-name",
    "C1_DATASET_VERSION": "canonical-100gb-v1",
    "C1_FEATURE_RUN_ID": "ab-1-optimized-feat-r1",
    "C1_FEAST_SNAPSHOT_ID": "snapshot-1",
    "C1_ONLINE_REGISTRY_ID": "online-1",
    "C1_TARGET_END": "2026-05-31T23:00:00Z",
    "C1_RUN_ID": "manual__2026-11-06T00:00:00+00:00",
    "FEAST_ONLINE_IMAGE": "registry/image@sha256:abc",
    "FEAST_ONLINE_GIT_SHA": "a" * 40,
}


def manifest(context, **table_overrides) -> bytes:
    tables = {
        view: {
            "target": f"{context.request.snapshot_root}/{spec['table']}",
            "entity_key": spec["entity_key"],
            "event_timestamp_type": "timestamp[us, tz=UTC]",
            "source_rows": 10,
            "output_rows": 10,
            **table_overrides,
        }
        for view, spec in FEATURE_SPECS.items()
    }
    return json.dumps({"dataset_version": "canonical-100gb-v1", "tables": tables}).encode()


class FakeReader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.reads = []

    def read(self, uri):
        self.reads.append(uri)
        return self.payload


class FakeCluster:
    """Simulate the operator: state, incremental and parity Jobs."""

    def __init__(self, context, end=T22, fail_next=None):
        self.context = context
        self.end = end
        self.jobs = []
        self.calls = []
        self.fail_next = list(fail_next or [])
        self.rendered = []

    def _state(self):
        return {
            "generation": 1,
            "views": [
                {
                    "view": name,
                    "materialized_end": self.end.isoformat() if self.end else None,
                    "source_path": f"{self.context.request.snapshot_root}/{spec['table']}",
                    "source_max_event_timestamp": SOURCE_MAX.isoformat(),
                }
                for name, spec in FEATURE_SPECS.items()
            ],
        }

    def list_window_jobs(self, window_id):
        return self.jobs

    def run_operator(self, name, window_id, arguments):
        self.calls.append((name, tuple(arguments)))
        if self.fail_next and self.fail_next[0] == arguments[0]:
            self.fail_next.pop(0)
            if arguments[0] == "materialize-incremental":
                self.jobs.append(ExistingJob(name, 0, 0, 1))
            raise StageError(f"operator Job {name} failed")
        if arguments[0] == "state":
            return self._state()
        if arguments[0] == "materialize-incremental":
            self.jobs.append(ExistingJob(name, 0, 1, 0))
            self.end = T23
            return {"mode": "incremental", "views": []}
        return parity(T22, T23)

    def render_operator(self, name, window_id, arguments):
        self.rendered.append((name, tuple(arguments)))
        return f"kind: Job\nname: {name}\n"


class WorkflowCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.context = self.make_context()
        self.reader = FakeReader(manifest(self.context))

    def make_context(self, **environment):
        values = {**ENVIRONMENT, "C1_EVIDENCE_ROOT": self.temporary.name, **environment}
        base = context_from_environment(values)
        execute = {"C1_MODE": "execute", "C1_APPROVAL": base.expected_approval}
        return context_from_environment({**values, **execute, **environment})

    def run_all(self, cluster, stages=STAGES, context=None):
        return [run_stage(stage, context or self.context, cluster, self.reader) for stage in stages]


class ExecuteWorkflowTest(WorkflowCase):
    def test_a_new_window_runs_every_stage_in_order(self):
        cluster = FakeCluster(self.context)
        results = self.run_all(cluster)
        self.assertEqual(["PASS"] * 7, [item["status"] for item in results])
        self.assertEqual(
            ["state", "materialize-incremental", "state", "parity"],
            [call[1][0] for call in cluster.calls],
        )
        self.assertEqual("NEW_WINDOW", results[-1]["outcome"])
        self.assertEqual(6, len(results[-1]["artifacts"]))

    def test_materialization_uses_the_requested_end(self):
        cluster = FakeCluster(self.context)
        self.run_all(cluster)
        self.assertIn(("materialize-incremental", "--end", "2026-05-31T23:00:00+00:00"),
                      [call[1] for call in cluster.calls])

    def test_a_finished_window_skips_the_job_and_still_verifies_readback(self):
        cluster = FakeCluster(self.context, end=T23)
        results = self.run_all(cluster)
        self.assertEqual("SKIPPED", results[3]["status"])
        self.assertEqual("PASS", results[5]["status"])
        self.assertEqual("COMPLETED", results[-1]["outcome"])
        self.assertNotIn("materialize-incremental", [call[1][0] for call in cluster.calls])

    def test_no_new_data_is_recorded_without_claiming_a_load(self):
        context = self.make_context(C1_TARGET_END="2026-06-01T01:00:00Z")
        self.reader = FakeReader(manifest(context))
        cluster = FakeCluster(context, end=T00)
        results = self.run_all(cluster, context=context)
        self.assertEqual("NO_NEW_DATA", results[-1]["outcome"])
        self.assertEqual(["SKIPPED"] * 3, [item["status"] for item in results[3:6]])
        self.assertEqual(["state"], [call[1][0] for call in cluster.calls])

    def test_a_never_materialized_registry_stops_before_any_write(self):
        cluster = FakeCluster(self.context, end=None)
        self.run_all(cluster, ("config", "source"))
        with self.assertRaises(WindowError):
            run_stage("window", self.context, cluster, self.reader)
        self.assertEqual(["state"], [call[1][0] for call in cluster.calls])

    def test_a_failed_job_stops_the_stage_and_writes_no_artifact(self):
        cluster = FakeCluster(self.context, fail_next=["materialize-incremental"])
        self.run_all(cluster, ("config", "source", "window"))
        with self.assertRaises(StageError):
            run_stage("materialize", self.context, cluster, self.reader)
        self.assertFalse((self.context.run_dir / ARTIFACTS["materialize"]).exists())

    def test_a_retry_after_failure_uses_the_next_attempt(self):
        cluster = FakeCluster(self.context, fail_next=["materialize-incremental"])
        self.run_all(cluster, ("config", "source", "window"))
        with self.assertRaises(StageError):
            run_stage("materialize", self.context, cluster, self.reader)
        result = run_stage("materialize", self.context, cluster, self.reader)
        self.assertEqual("PASS", result["status"])
        self.assertTrue(result["job_name"].endswith("-a2"))

    def test_a_retry_joins_a_job_left_running_by_a_dead_worker(self):
        cluster = FakeCluster(self.context)
        self.run_all(cluster, ("config", "source", "window"))
        running = f"feast-inc-{self.context.request.window_id}-a1"
        cluster.jobs.append(ExistingJob(running, 1, 0, 0))
        result = run_stage("materialize", self.context, cluster, self.reader)
        self.assertEqual("ATTACH", result["job_action"])
        self.assertEqual(running, result["job_name"])

    def test_a_finished_stage_is_reused_on_retry(self):
        cluster = FakeCluster(self.context)
        first = self.run_all(cluster, ("config", "source", "window", "materialize"))[-1]
        calls = len(cluster.calls)
        self.assertEqual(first, run_stage("materialize", self.context, cluster, self.reader))
        self.assertEqual(calls, len(cluster.calls))

    def test_the_window_decision_is_frozen_for_the_run(self):
        cluster = FakeCluster(self.context)
        first = self.run_all(cluster, ("config", "source", "window"))[-1]
        cluster.end = T23
        self.assertEqual(first, run_stage("window", self.context, cluster, self.reader))

    def test_a_registry_that_did_not_advance_fails_completion(self):
        cluster = FakeCluster(self.context)
        self.run_all(cluster, ("config", "source", "window", "materialize"))
        cluster.end = T22
        with self.assertRaisesRegex(StageError, "did not reach"):
            run_stage("completion", self.context, cluster, self.reader)

    def test_a_readback_failure_fails_the_run(self):
        cluster = FakeCluster(self.context)
        self.run_all(cluster, ("config", "source", "window", "materialize", "completion"))
        cluster.run_operator = lambda *_: parity(T22, T23, status="FAIL")
        with self.assertRaises(ReadbackError):
            run_stage("readback", self.context, cluster, self.reader)
        with self.assertRaisesRegex(StageError, "readback"):
            run_stage("provenance", self.context, cluster, self.reader)

    def test_artifacts_of_another_request_are_refused(self):
        cluster = FakeCluster(self.context)
        self.run_all(cluster, ("config",))
        other = self.make_context(C1_FEAST_SNAPSHOT_ID="snapshot-2")
        with self.assertRaisesRegex(StageError, "different request"):
            run_stage("config", other, cluster, self.reader)

    def test_downstream_stages_need_upstream_artifacts(self):
        with self.assertRaisesRegex(StageError, "no artifact"):
            run_stage("materialize", self.context, FakeCluster(self.context), self.reader)

    def test_the_provenance_digest_matches_the_record(self):
        from ai_platform.features.feast.materialization_plan import provenance_digest

        record = self.run_all(FakeCluster(self.context))[-1]
        stored = json.loads((self.context.run_dir / ARTIFACTS["provenance"]).read_text())
        subset = {key: stored[key] for key in (
            "schema_version", "status", "outcome", "request", "manifest_sha256",
            "operator_image", "git_sha", "airflow", "artifacts")}
        self.assertEqual(record["provenance_digest"], provenance_digest(subset))


class ApprovalTest(WorkflowCase):
    def test_execute_mode_needs_the_token_of_this_window(self):
        context = self.make_context(C1_APPROVAL="approve-000000000000")
        with self.assertRaisesRegex(StageError, "C1_APPROVAL"):
            run_stage("config", context, FakeCluster(context), self.reader)

    def test_a_dry_run_changes_nothing_and_prints_the_token(self):
        context = self.make_context(C1_MODE="dry_run", C1_APPROVAL="")
        cluster = FakeCluster(context)
        results = self.run_all(cluster, context=context)
        self.assertEqual([], cluster.calls)
        self.assertEqual(2, len(cluster.rendered))
        self.assertEqual(context.expected_approval, results[0]["approval_token_for_this_window"])
        self.assertEqual("DRY_RUN", results[-1]["outcome"])


class ManifestValidationTest(WorkflowCase):
    def check(self, payload, expected=""):
        return validate_export_manifest(self.context.request, payload, expected)

    def test_a_matching_manifest_passes(self):
        result = self.check(manifest(self.context))
        self.assertEqual(4, len(result["rows"]))

    def test_a_pinned_digest_must_match(self):
        with self.assertRaisesRegex(StageError, "pinned"):
            self.check(manifest(self.context), "0" * 64)

    def test_each_identity_field_is_enforced(self):
        cases = (
            {"target": "gs://other/table"},
            {"entity_key": "other"},
            {"event_timestamp_type": "timestamp[us]"},
            {"output_rows": 9},
            {"output_rows": 0, "source_rows": 0},
        )
        for overrides in cases:
            with self.subTest(overrides), self.assertRaises(StageError):
                self.check(manifest(self.context, **overrides))

    def test_another_dataset_is_rejected(self):
        value = json.loads(manifest(self.context))
        value["dataset_version"] = "other"
        with self.assertRaisesRegex(StageError, "dataset"):
            self.check(json.dumps(value).encode())

    def test_a_missing_table_is_rejected(self):
        value = json.loads(manifest(self.context))
        value["tables"].pop("device_behavior")
        with self.assertRaisesRegex(StageError, "four"):
            self.check(json.dumps(value).encode())


class ConfigurationTest(unittest.TestCase):
    def test_required_values_are_enforced(self):
        for name in ENVIRONMENT:
            if name.startswith("C1_"):
                values = {**ENVIRONMENT, "C1_EVIDENCE_ROOT": "/tmp/x"}
                values.pop(name)
                with self.subTest(name), self.assertRaises(StageError):
                    context_from_environment(values)

    def test_an_unknown_mode_is_rejected(self):
        with self.assertRaises(StageError):
            context_from_environment({**ENVIRONMENT, "C1_EVIDENCE_ROOT": "/tmp/x", "C1_MODE": "run"})

    def test_the_run_id_cannot_escape_the_evidence_root(self):
        context = context_from_environment(
            {**ENVIRONMENT, "C1_EVIDENCE_ROOT": "/tmp/x", "C1_RUN_ID": "../../etc/x"}
        )
        self.assertEqual(Path("/tmp/x"), context.run_dir.parent)


class OperatorEvidenceTest(unittest.TestCase):
    def test_only_the_operator_schema_is_accepted(self):
        good = json.dumps({"schema_version": "feast-online-operation-evidence-v1"})
        self.assertEqual("feast-online-operation-evidence-v1",
                         parse_operator_evidence(good, "job")["schema_version"])
        for line in ("not json", "[]", '{"schema_version": "other"}'):
            with self.subTest(line), self.assertRaises(StageError):
                parse_operator_evidence(line, "job")


if __name__ == "__main__":
    unittest.main()
