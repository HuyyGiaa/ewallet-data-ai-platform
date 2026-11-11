"""Feast selection evidence derived from a real DP3 completion handoff."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from ai_platform.features.feast.provenance import (
    REHEARSAL_DP3_BASIS,
    build_feature_selection_evidence,
    load_feature_selection_evidence,
    main,
    selection_from_dp3_completion,
)

ROOT = Path(__file__).resolve().parents[2]
HANDOFF = (
    ROOT
    / "docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456/manifests/optimized"
    / "dp3_completion.json"
)
FEATURE_RUN = "ab-20261110225456-optimized-feat-r261110225459"


class SelectionFromDp3CompletionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = HANDOFF.read_bytes()

    def mutated(self, change) -> bytes:
        value = json.loads(self.raw)
        change(value)
        return json.dumps(value).encode()

    def test_rehearsal_handoff_yields_bound_selection(self) -> None:
        value = selection_from_dp3_completion(self.raw)
        self.assertEqual(value["selection_basis"], REHEARSAL_DP3_BASIS)
        self.assertEqual(value["dataset_version"], "rehearsal-5gb-v1")
        self.assertEqual(value["source_feature_run_id"], FEATURE_RUN)
        self.assertEqual(
            value["source_silver_run_id"],
            "ab-20261110225456-optimized-silver-r261110225457",
        )
        self.assertEqual(value["spark_profile"], "optimized")
        self.assertEqual(
            value["source_evidence_digest"],
            "sha256:" + hashlib.sha256(self.raw).hexdigest(),
        )

    def test_cli_writes_loadable_create_only_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "dp3.json")
            source.write_bytes(self.raw)
            output = Path(tmp, "selection.json")
            with unittest.mock.patch("builtins.print"):
                self.assertEqual(
                    main(["--dp3-completion", str(source), "--output", str(output)]), 0
                )
            self.assertEqual(load_feature_selection_evidence(output).feature_run_id, FEATURE_RUN)
            with self.assertRaises(FileExistsError):
                main(["--dp3-completion", str(source), "--output", str(output)])

    def test_failed_validation_or_batch_is_refused(self) -> None:
        def fail_validation(value):
            value["stages"]["features"]["validation"]["status"] = "FAIL"

        def fail_batch(value):
            value["stages"]["features"]["execution_metadata"]["dataproc_batch_state"] = "FAILED"

        for change in (fail_validation, fail_batch):
            with self.subTest(change.__name__), self.assertRaises(ValueError):
                selection_from_dp3_completion(self.mutated(change))

    def test_foreign_root_bucket_or_run_is_refused(self) -> None:
        def other_run(value):
            value["stages"]["features"]["output_root"] = value["stages"]["features"][
                "output_root"
            ].replace(FEATURE_RUN, "ab-20261110225456-baseline-feat-r261110225459")

        def other_bucket(value):
            value["stages"]["features"]["output_root"] = value["stages"]["features"][
                "output_root"
            ].replace("rehearsal-5gb-data", "data")
            value["data_bucket"] = "ewallet-data-ai-platform-hg26-data"

        def two_silver(value):
            roots = value["stages"]["features"]["execution_metadata"]["input_roots"]
            roots.append(roots[0])

        for change in (other_run, other_bucket, two_silver):
            with self.subTest(change.__name__), self.assertRaises(ValueError):
                selection_from_dp3_completion(self.mutated(change))

    def test_rehearsal_basis_cannot_label_canonical_data(self) -> None:
        with self.assertRaises(ValueError):
            build_feature_selection_evidence(
                dataset_version="canonical-100gb-v1",
                silver_run_id="canonical-silver-20261024-010203",
                feature_run_id="canonical-features-20261024-010203",
                feature_root="gs://b/lakehouse/runs/canonical-features-20261024-010203/gold",
                spark_profile="optimized",
                git_sha="a" * 40,
                selection_basis=REHEARSAL_DP3_BASIS,
                source_evidence_digest="sha256:" + "b" * 64,
            )


if __name__ == "__main__":
    unittest.main()
