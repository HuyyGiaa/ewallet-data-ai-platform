"""Static contracts keeping the G2 source gate separate from I7 ingestion."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
DAGS = ROOT / "data_platform/orchestration/airflow/dags"
ISOLATED_RUNNER = ROOT / "scripts/validation/run_rag_dag_isolated.sh"


def _source(name: str) -> str:
    return (DAGS / name).read_text(encoding="utf-8")


def _dag_id_and_schedule(source: str) -> tuple[str, object]:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "DAG":
            values = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords
                      if kw.arg in {"dag_id", "schedule"}}
            return values["dag_id"], values["schedule"]
    raise AssertionError("DAG declaration not found")


class RagDagReconciliationTests(unittest.TestCase):
    def test_dag_ids_are_unique_and_both_are_manual(self) -> None:
        source_gate = _dag_id_and_schedule(_source("rag_fraud_policy.py"))
        ingestion = _dag_id_and_schedule(_source("rag_fraud_policy_ingestion.py"))
        self.assertEqual(source_gate, ("rag_fraud_policy", None))
        self.assertEqual(ingestion, ("rag_fraud_policy_ingestion", None))
        self.assertNotEqual(source_gate[0], ingestion[0])

    def test_g2_dag_builds_evidence_but_never_publishes_runtime_state(self) -> None:
        source = _source("rag_fraud_policy.py")
        self.assertIn("build_feast_publication_artifact", source)
        self.assertIn("evaluate_retrieval_with_citations", source)
        self.assertNotIn("ops.llm_agent.rag_ingest", source)
        self.assertNotIn("publish_feast_features", source)
        self.assertNotIn("publish_datahub_lineage", source)

    def test_i7_dag_is_the_only_apply_materialize_and_lineage_path(self) -> None:
        source = _source("rag_fraud_policy_ingestion.py")
        for task, stage in (
            ("discover_documents", "discover"),
            ("parse_and_chunk", "chunk"),
            ("generate_embeddings", "embed"),
            ("publish_feast_features", "publish"),
            ("validate_online_readback", "validate"),
            ("publish_datahub_lineage", "datahub"),
        ):
            self.assertIn(f'(\"{task}\", \"{stage}\"', source)
        self.assertIn("rag_stage_command(stage)", source)

    def test_isolated_runtime_runner_executes_the_i7_ingestion_dag(self) -> None:
        source = ISOLATED_RUNNER.read_text(encoding="utf-8")
        self.assertIn('DAG_ID="rag_fraud_policy_ingestion"', source)
        self.assertIn('run_airflow dags test "${DAG_ID}"', source)
        self.assertIn('tasks states-for-dag-run "${DAG_ID}"', source)
        self.assertNotIn("run_airflow dags test rag_fraud_policy ", source)


if __name__ == "__main__":
    unittest.main()
