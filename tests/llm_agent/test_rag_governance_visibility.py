"""DataHub flow/job shape and the search/lineage index visibility gate."""

from __future__ import annotations

import unittest
from unittest import mock

from ai_platform.llm_agent.rag import governance

FEAST = governance.feast_urn("ewallet_rag_verify", "f1")
CORPUS = governance.corpus_urn("data/llm_agent/rag/fraud-policy-v1", "c1")
PUBLISH_JOB = governance.job_urn(governance.DAG_ID, "publish_feast_features")
FLOW = governance.flow_urn(governance.DAG_ID)


def index(contents):
    """A fake GraphQL endpoint backed by {(query kind, key): [urns]}."""
    calls = []

    def run(query, variables):
        calls.append(variables)
        if "searchAcrossLineage" in query:
            urns = contents.get(("lineage", variables["urn"]), [])
            return {"searchAcrossLineage": {"searchResults": [{"entity": {"urn": u}} for u in urns]}}
        urns = contents.get(("search", variables["type"]), [])
        return {"search": {"searchResults": [{"entity": {"urn": u}} for u in urns]}}

    return run, calls


class FlowShapeTests(unittest.TestCase):
    def test_every_airflow_task_is_a_datajob_of_the_canonical_flow(self):
        records = governance.lineage_records(
            documents={"source_location": "data/llm_agent/rag/fraud-policy-v1", "corpus_revision": "c1",
                       "document_count": 2, "manifest_sha256": "m"},
            embeddings={"model_id": "m", "model_revision": "r", "dimensions": 384},
            publication={"feature_index_revision": "f1", "row_count": 4, "publication_identity": "p",
                         "feast": {"project": "ewallet_rag_verify", "offline_source": "x", "online_store": "redis"}},
            run_id="run", git_sha="g")
        jobs = sorted(urn for urn, aspect, _ in records if aspect == "dataJobInfo")
        expected = sorted(governance.job_urn(governance.DAG_ID, task) for task in (
            "discover_documents", "parse_and_chunk", "generate_embeddings", "publish_feast_features",
            "validate_online_readback", "publish_datahub_lineage"))
        self.assertEqual(jobs, expected)
        self.assertIn("rag_fraud_policy_ingestion", FLOW)
        io = {urn: payload for urn, aspect, payload in records if aspect == "dataJobInputOutput"}
        self.assertEqual(io[governance.job_urn(governance.DAG_ID, "publish_datahub_lineage")],
                         {"inputDatasets": [], "outputDatasets": []})
        self.assertEqual(len(records), 5 + 2 * 6)


class VisibilityTests(unittest.TestCase):
    def test_visible_when_search_and_lineage_return_every_entity(self):
        run, _ = index({("search", "DATASET"): [FEAST, CORPUS], ("search", "DATA_FLOW"): [FLOW],
                        ("lineage", FEAST): [CORPUS, PUBLISH_JOB]})
        result = governance.verify_index_visibility(run, feast_dataset=FEAST, corpus_dataset=CORPUS,
                                                    attempts=1, delay_seconds=0)
        self.assertEqual(result["status"], "VISIBLE")
        self.assertEqual(set(result["checks"].values()), {"FOUND"})

    def test_stored_but_unindexed_metadata_is_not_visible(self):
        # GMS has the aspects, but the index never received them (e.g. read-only OpenSearch).
        run, calls = index({("search", "DATASET"): [FEAST, CORPUS], ("search", "DATA_FLOW"): [FLOW]})
        with mock.patch.object(governance.time, "sleep") as sleep:
            result = governance.verify_index_visibility(run, feast_dataset=FEAST, corpus_dataset=CORPUS,
                                                        attempts=3, delay_seconds=1)
        self.assertEqual(result["status"], "NOT_VISIBLE")
        self.assertEqual(result["checks"]["lineage_corpus_upstream_of_feast"], "MISSING")
        self.assertEqual(result["checks"]["feast_dataset_searchable"], "FOUND")
        self.assertEqual(sleep.call_count, 2)
        self.assertGreater(len(calls), 5)

    def test_unrelated_search_hits_do_not_count(self):
        other = governance.feast_urn("ewallet_rag", "f1")
        run, _ = index({("search", "DATASET"): [other, CORPUS], ("search", "DATA_FLOW"): [FLOW],
                        ("lineage", FEAST): [CORPUS, PUBLISH_JOB]})
        result = governance.verify_index_visibility(run, feast_dataset=FEAST, corpus_dataset=CORPUS,
                                                    attempts=1, delay_seconds=0)
        self.assertEqual(result["checks"]["feast_dataset_searchable"], "MISSING")


if __name__ == "__main__":
    unittest.main()
