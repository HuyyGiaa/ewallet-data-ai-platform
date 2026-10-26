from __future__ import annotations

import hashlib
import json
import unittest

from ops.gcp.summarize_spark_output import semantic_fingerprint_payload
from ops.gcp.prepare_spark_stage_pair import build_stage_pair_manifest
from ops.gcp.validate_spark_output_equivalence import compare_summaries


def fixture_hash_pair(value):
    canonical = json.dumps(
        {"type": type(value).__name__, "value": value},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).digest()
    return (
        int.from_bytes(digest[:8], "big", signed=True),
        int.from_bytes(digest[8:12], "big", signed=True),
    )


def fixture_fingerprint(values):
    pairs = [fixture_hash_pair(value) for value in values]
    return semantic_fingerprint_payload(
        rows=len(pairs),
        xxhash_sum=str(sum(left for left, _ in pairs)),
        murmur_sum=str(sum(right for _, right in pairs)),
        xxhash_min=min((left for left, _ in pairs), default=0),
        xxhash_max=max((left for left, _ in pairs), default=0),
    )


def pair_manifest():
    benchmark = "ab-20261025123456"
    attempt = "r261025123456"
    baseline = f"{benchmark}-baseline-bronze-{attempt}"
    optimized = f"{benchmark}-optimized-bronze-{attempt}"
    return build_stage_pair_manifest(
        stage="dp1-bronze",
        dataset_version="canonical-100gb-v1",
        git_sha="a" * 40,
        upstream_run_ids=("canonical-100gb-v1",),
        baseline_output_run_id=baseline,
        baseline_output_root=f"gs://bucket/lakehouse/runs/{baseline}/bronze",
        candidate_output_run_id=optimized,
        candidate_output_root=f"gs://bucket/lakehouse/runs/{optimized}/bronze",
    )


def summary(manifest, profile, *, fingerprint="same", schema="schema"):
    member = next(
        run for run in manifest["runs"] if run["spark_profile"] == profile
    )
    table = {
        "schema_sha256": schema,
        "row_count": 10,
        "semantic_content_fingerprint": fingerprint,
        "required_null_counts": {"transaction_id": 0},
        "partition_columns": [],
        "physical_layout": {
            "file_count": 1, "total_bytes": 1, "file_size_min": 1,
            "file_size_median": 1, "file_size_p95": 1, "file_size_max": 1,
        },
    }
    tables = {
        name: dict(table)
        for name in (
            "users", "accounts", "merchants", "devices", "transactions",
            "balance_snapshots", "login_events", "fraud_labels",
        )
    }
    return {
        "pair_manifest_digest": manifest["pair_manifest_digest"],
        "benchmark_id": manifest["benchmark_id"],
        "dataset_version": "canonical-100gb-v1",
        "git_sha": "a" * 40,
        "source_run_ids": list(manifest["upstream_run_ids"]),
        "upstream_run_ids": list(manifest["upstream_run_ids"]),
        "spark_profile": profile,
        "stage": "dp1-bronze",
        "output_run_id": member["output_run_id"],
        "output_root": member["output_root"],
        "spark_properties": member["spark_properties"],
        "spark_properties_digest": member["spark_properties_digest"],
        "tables": tables,
        "invariants": {"source_coverage": True, "source_counts_match": True},
    }


class SparkSemanticEquivalenceTest(unittest.TestCase):
    def test_commutative_fingerprint_ignores_row_and_partition_order(self) -> None:
        expected = fixture_fingerprint([None, "", 0, "x"])
        self.assertEqual(expected, fixture_fingerprint(["x", 0, None, ""]))
        self.assertEqual(expected, fixture_fingerprint([None, ""] + [0, "x"]))

    def test_fingerprint_detects_values_added_removed_and_null_changes(self) -> None:
        original = fixture_fingerprint([None, "", 0, "x"])
        self.assertNotEqual(original, fixture_fingerprint([None, "", 1, "x"]))
        self.assertNotEqual(original, fixture_fingerprint([None, "", 0, "x", "y"]))
        self.assertNotEqual(original, fixture_fingerprint([None, "", 0]))
        self.assertEqual(3, len({fixture_fingerprint([None]), fixture_fingerprint([""]), fixture_fingerprint([0])}))

    def test_equivalence_fails_closed_for_schema_content_or_lineage(self) -> None:
        manifest = pair_manifest()
        baseline = summary(manifest, "baseline")
        candidate = summary(manifest, "optimized")
        self.assertTrue(compare_summaries(
            baseline, candidate, pair_manifest=manifest,
            baseline_evidence=baseline, optimized_evidence=candidate,
        )["high_confidence_equivalence"])

        for mutation in ("schema", "fingerprint", "lineage"):
            changed = summary(manifest, "optimized")
            if mutation == "schema":
                changed["tables"]["transactions"]["schema_sha256"] = "changed"
            elif mutation == "fingerprint":
                changed["tables"]["transactions"]["semantic_content_fingerprint"] = "changed"
            else:
                changed["source_run_ids"] = ["different-source"]
                changed["upstream_run_ids"] = ["different-source"]
            if mutation == "lineage":
                with self.assertRaisesRegex(ValueError, "upstream_run_ids"):
                    compare_summaries(
                        baseline, changed, pair_manifest=manifest,
                        baseline_evidence=baseline, optimized_evidence=changed,
                    )
            else:
                self.assertFalse(compare_summaries(
                    baseline, changed, pair_manifest=manifest,
                    baseline_evidence=baseline, optimized_evidence=changed,
                )["high_confidence_equivalence"])


if __name__ == "__main__":
    unittest.main()
