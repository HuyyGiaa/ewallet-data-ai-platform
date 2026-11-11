"""Current-run Feast materialization lineage and semantic readback."""

from __future__ import annotations

import copy
import unittest

from ai_platform.features.feast.schema import FEATURE_SPECS

from data_platform.metadata.datahub.lineage.feast_materialization import (
    aspect_records,
    build_plan,
    publish_with_readback,
)


def provenance() -> dict[str, object]:
    return {
        "schema_version": "feast-incremental-provenance-v1",
        "status": "PASS",
        "outcome": "NEW_WINDOW",
        "window_id": "abc123def456",
        "provenance_digest": "sha256:provenance",
        "git_sha": "a" * 40,
        "window_decision": {
            "state": "NEW_WINDOW",
            "start": "2026-11-10T23:00:00+00:00",
            "end": "2026-11-11T00:00:00+00:00",
            "pending_views": list(FEATURE_SPECS),
            "reason": "new window",
        },
        "request": {
            "bucket": "ewallet-data-ai-platform-hg26-rehearsal-5gb-data",
            "dataset_version": "rehearsal-5gb-v1",
            "feature_run_id": "rehearsal-feat-r1",
            "feast_snapshot_id": "snapshot-r1",
            "online_registry_id": "online-r1",
            "online_store_id": "rehearsal-online",
            "snapshot_root": (
                "gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data/features/"
                "offline/snapshots/rehearsal-5gb-v1/rehearsal-feat-r1/snapshot-r1"
            ),
            "registry_uri": (
                "gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data/features/"
                "registries/rehearsal-5gb-v1/online/online-r1/registry.db"
            ),
            "target_end": "2026-11-11T00:00:00+00:00",
        },
        "airflow": {
            "dag_id": "feast_incremental_materialization",
            "run_id": "manual__2026-11-11T00:00:00+00:00",
        },
    }


class MemoryAdapter:
    def __init__(self, corrupt: bool = False) -> None:
        self.values = {}
        self.corrupt = corrupt

    def put(self, urn, aspect, payload) -> None:
        self.values[(urn, aspect)] = payload

    def get(self, urn, aspect):
        value = self.values.get((urn, aspect))
        return {**value, "name": "wrong"} if self.corrupt and aspect == "dataFlowInfo" else value


class FeastLineageTest(unittest.TestCase):
    def test_current_run_builds_four_exact_dataset_edges_and_one_job(self) -> None:
        plan = build_plan(provenance())
        self.assertEqual(4, len(plan["dataset_lineage"]))
        self.assertEqual(1, len(plan["jobs"]))
        self.assertEqual(4, len(plan["jobs"][0]["inputs"]))
        self.assertEqual(4, len(plan["jobs"][0]["outputs"]))
        for edge in plan["dataset_lineage"]:
            self.assertEqual(1, len(edge["upstreams"]))
            self.assertIn("rehearsal-5gb-v1", edge["downstream"])
            self.assertNotIn("abc123def456", edge["downstream"])

    def test_dataset_and_job_urns_are_stable_across_windows(self) -> None:
        first = build_plan(provenance())
        second_provenance = copy.deepcopy(provenance())
        second_provenance["window_id"] = "fff123def456"
        second_provenance["airflow"]["run_id"] = "manual__later"
        second = build_plan(second_provenance)
        self.assertEqual(first["datasets"], second["datasets"])
        self.assertEqual(first["jobs"][0]["job_urn"], second["jobs"][0]["job_urn"])

    def test_partial_lineage_names_only_the_materialized_views(self) -> None:
        value = provenance()
        value["outcome"] = "PARTIAL"
        value["window_decision"]["state"] = "PARTIAL"
        value["window_decision"]["pending_views"] = ["device_behavior"]
        plan = build_plan(value)
        self.assertEqual(1, len(plan["dataset_lineage"]))
        self.assertEqual(1, len(plan["jobs"][0]["inputs"]))
        self.assertEqual(1, len(plan["jobs"][0]["outputs"]))
        self.assertIn("device_behavior", plan["dataset_lineage"][0]["downstream"])

    def test_job_metadata_uses_actual_dagrun_and_provenance(self) -> None:
        records = aspect_records(build_plan(provenance()))
        flow = next(payload for _, aspect, payload in records if aspect == "dataFlowInfo")
        job = next(payload for _, aspect, payload in records if aspect == "dataJobInfo")
        self.assertEqual("manual__2026-11-11T00:00:00+00:00", flow["customProperties"]["dag_run_id"])
        self.assertEqual("sha256:provenance", job["customProperties"]["provenance_digest"])
        self.assertEqual("a" * 40, job["customProperties"]["git_sha"])
        self.assertEqual("NEW_WINDOW", job["customProperties"]["materialization_outcome"])

    def test_historical_hardcoded_campaign_identifiers_are_absent(self) -> None:
        rendered = repr(build_plan(provenance()))
        self.assertNotIn("ab-202610", rendered)
        self.assertNotIn("bbae48ca", rendered)

    def test_every_published_aspect_requires_equal_readback(self) -> None:
        plan = build_plan(provenance())
        result = publish_with_readback(plan, MemoryAdapter())
        self.assertEqual(len(aspect_records(plan)), result["published"])
        self.assertTrue(all(item["readback"] == "MATCH" for item in result["verified"]))
        with self.assertRaisesRegex(RuntimeError, "readback mismatch"):
            publish_with_readback(plan, MemoryAdapter(corrupt=True))

    def test_mismatched_or_incomplete_provenance_fails_closed(self) -> None:
        for mutate in (
            lambda value: value.update(status="FAIL"),
            lambda value: value.update(outcome="UNKNOWN"),
            lambda value: value["request"].update(dataset_version="canonical-100gb-v1"),
            lambda value: value["airflow"].pop("run_id"),
        ):
            value = copy.deepcopy(provenance())
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_plan(value)


if __name__ == "__main__":
    unittest.main()
