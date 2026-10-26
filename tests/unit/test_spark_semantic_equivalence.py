from __future__ import annotations

import hashlib
import json
import unittest

from ops.gcp.summarize_spark_output import semantic_fingerprint_payload
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


def summary(*, fingerprint="same", schema="schema", source=("source-run",)):
    table = {
        "schema_sha256": schema,
        "row_count": 10,
        "semantic_content_fingerprint": fingerprint,
        "required_null_counts": {"transaction_id": 0},
        "partition_columns": [],
    }
    tables = {
        name: dict(table)
        for name in (
            "users", "accounts", "merchants", "devices", "transactions",
            "balance_snapshots", "login_events", "fraud_labels",
        )
    }
    return {
        "benchmark_id": "ab-20261025123456",
        "dataset_version": "canonical-100gb-v1",
        "git_sha": "a" * 40,
        "source_run_ids": list(source),
        "spark_profile": "baseline",
        "stage": "dp1-bronze",
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
        baseline = summary()
        candidate = summary()
        candidate["spark_profile"] = "optimized"
        self.assertTrue(compare_summaries(baseline, candidate)["high_confidence_equivalence"])

        for mutation in ("schema", "fingerprint", "lineage"):
            changed = summary()
            changed["spark_profile"] = "optimized"
            if mutation == "schema":
                changed["tables"]["transactions"]["schema_sha256"] = "changed"
            elif mutation == "fingerprint":
                changed["tables"]["transactions"]["semantic_content_fingerprint"] = "changed"
            else:
                changed["source_run_ids"] = ["different-source"]
            if mutation == "lineage":
                with self.assertRaisesRegex(ValueError, "source_run_ids"):
                    compare_summaries(baseline, changed)
            else:
                self.assertFalse(compare_summaries(baseline, changed)["high_confidence_equivalence"])


if __name__ == "__main__":
    unittest.main()
