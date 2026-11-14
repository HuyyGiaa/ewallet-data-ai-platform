"""One canonical RAG ingestion DAG, named identically everywhere it is used."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
DAGS = ROOT / "data_platform/orchestration/airflow/dags"
CANONICAL = "rag_fraud_policy_ingestion"
STAGES = (
    ("discover_documents", "discover"),
    ("parse_and_chunk", "chunk"),
    ("generate_embeddings", "embed"),
    ("publish_feast_features", "publish"),
    ("validate_online_readback", "validate"),
    ("publish_datahub_lineage", "datahub"),
)


def _dag_declarations() -> dict[str, tuple[str, object]]:
    found = {}
    for path in sorted(DAGS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "DAG":
                values = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords
                          if kw.arg in {"dag_id", "schedule"}}
                found[values["dag_id"]] = (path.name, values.get("schedule"))
    return found


class RagDagReconciliationTests(unittest.TestCase):
    def test_exactly_one_manual_rag_dag_exists(self) -> None:
        rag = {dag_id: value for dag_id, value in _dag_declarations().items() if dag_id.startswith("rag_")}
        self.assertEqual(rag, {CANONICAL: (f"{CANONICAL}.py", None)})
        self.assertFalse((DAGS / "rag_fraud_policy.py").exists())
        self.assertFalse((ROOT / "data_platform/orchestration/airflow/rag_ingestion_params.py").exists())

    def test_canonical_dag_runs_the_six_stages_in_order(self) -> None:
        source = (DAGS / f"{CANONICAL}.py").read_text(encoding="utf-8")
        order = [match.group(1) for match in re.finditer(r'\("(\w+)", "\w+", timedelta', source)]
        self.assertEqual(order, [task for task, _ in STAGES])
        for task, stage in STAGES:
            self.assertIn(f'("{task}", "{stage}"', source)
        self.assertIn("rag_stage_command(stage)", source)
        self.assertIn("previous >> task", source)

    def test_every_consumer_names_the_canonical_dag(self) -> None:
        from ai_platform.llm_agent.rag import governance

        self.assertEqual(governance.DAG_ID, CANONICAL)
        consumers = {
            "scripts/llm_agent/rag_runtime_run.sh": f"DAG_ID={CANONICAL}",
            "scripts/validation/run_rag_dag_isolated.sh": f'DAG_ID="{CANONICAL}"',
            "scripts/validation/rag_dag_isolated_parse.py": f'DAG_ID = "{CANONICAL}"',
            "scripts/validation/validate_rag_dag_import.sh": f"dags/{CANONICAL}.py",
            "Jenkinsfile.rag": f"/api/v2/dags/{CANONICAL}/dagRuns",
        }
        for path, expected in consumers.items():
            with self.subTest(path=path):
                source = (ROOT / path).read_text(encoding="utf-8")
                self.assertIn(expected, source)
                self.assertIsNone(re.search(r"\brag_fraud_policy\b(?!_ingestion)", source))


if __name__ == "__main__":
    unittest.main()
