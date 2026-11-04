from __future__ import annotations

import json
from pathlib import Path
import unittest

from scripts.ci.verify_rag_smoke import verify


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "jenkins/cd/rag-pipeline.contract.json").read_text())
SHA = "c" * 40


def summary(**overrides: object) -> str:
    body = {
        "release_sha": SHA, "corpus_revision": CONTRACT["corpus_revision"], "chunk_count": 4,
        "embedding_model": CONTRACT["embedding_model"], "embedding_revision": CONTRACT["embedding_revision"],
        "embedding_dimensions": 384, "published_rows": 4,
        "publication_identity": CONTRACT["publication_identity"],
        "feast_target": {"online_store": "redis", "project": "ewallet_rag_cd"},
        "validation": {"readback_status": "MATCH", "recall_at_k": 1.0, "citation_errors": 0,
                       "empty_retrieval_rate": 0.0},
    }
    body.update(overrides)
    return "noise\nRAG_SMOKE_SUMMARY " + json.dumps(body) + "\n"


class VerifyRagSmokeTests(unittest.TestCase):
    def test_matching_run_passes(self) -> None:
        self.assertEqual(verify(summary(), CONTRACT, SHA)["verdict"], "PASS")

    def test_changed_chunking_or_embedding_fails(self) -> None:
        for override in ({"publication_identity": "0" * 64}, {"embedding_revision": "x"},
                         {"chunk_count": 5}, {"embedding_dimensions": 768}):
            with self.subTest(override=override):
                result = verify(summary(**override), CONTRACT, SHA)
                self.assertEqual(result["verdict"], "FAIL")

    def test_wrong_release_sha_and_missing_summary_fail(self) -> None:
        self.assertEqual(verify(summary(release_sha="d" * 40), CONTRACT, SHA)["verdict"], "FAIL")
        self.assertEqual(verify("no summary here", CONTRACT, SHA)["verdict"], "FAIL")

    def test_retrieval_regression_fails(self) -> None:
        bad = {"readback_status": "MATCH", "recall_at_k": 0.5, "citation_errors": 1, "empty_retrieval_rate": 0.0}
        result = verify(summary(validation=bad), CONTRACT, SHA)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("recall_at_k", " ".join(result["failures"]))


if __name__ == "__main__":
    unittest.main()
