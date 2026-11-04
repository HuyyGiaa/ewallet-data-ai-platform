from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.ci.catalog import load_catalog
from scripts.ci.change_detection import detect_scope, resolve_git_scope


ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "jenkins/catalog/components.json")


class PathScopeTests(unittest.TestCase):
    def test_direct_path_selects_only_component_without_dependents(self) -> None:
        scope = detect_scope(CATALOG, ["ai_platform/llm_agent/byo_coordinator.py"])
        self.assertEqual(scope.components, ("agents",))
        self.assertIsNone(scope.full_validation_reason)

    def test_fraud_api_library_changes_revalidate_the_mcp_image_and_agents(self) -> None:
        # feature-rag-mcp/drift-mcp import ai_platform/inference (Feast Online,
        # 41-input wire, KServe client) and the ML training contract at runtime.
        for path in (
            "ai_platform/inference/kserve.py",
            "ai_platform/inference/feast_online.py",
            "ai_platform/inference/wire.py",
            "ai_platform/ml/training/contract.py",
            "ai_platform/ml/training/derived.py",
        ):
            with self.subTest(path=path):
                scope = detect_scope(CATALOG, [path])
                self.assertTrue({"fraud-api", "mcp-agents", "agents"} <= set(scope.components))
                self.assertIsNone(scope.full_validation_reason)

    def test_upstream_change_adds_reverse_dependency_closure(self) -> None:
        scope = detect_scope(CATALOG, ["ai_platform/features/feast/schema.py"])
        self.assertEqual(
            set(scope.components),
            {"feast", "ml-training", "mlflow", "fraud-serving", "fraud-api", "rag-pipeline", "mcp-agents", "agents"},
        )
        self.assertNotIn("streaming", scope.components)

    def test_shared_path_selects_every_active_component(self) -> None:
        scope = detect_scope(CATALOG, ["scripts/ci/catalog.py"])
        self.assertEqual(scope.components, CATALOG.active_components)
        self.assertEqual(scope.full_validation_reason, "shared_path_changed")

    def test_documentation_only_path_selects_no_runtime_component(self) -> None:
        scope = detect_scope(CATALOG, ["README.md"])
        self.assertEqual(scope.components, ())
        self.assertEqual(scope.full_validation_reason, "non_runtime_paths_only")

    def test_similar_directory_name_does_not_bypass_full_validation(self) -> None:
        scope = detect_scope(CATALOG, ["data_platform_backup/unsafe.py"])
        self.assertEqual(scope.components, CATALOG.active_components)
        self.assertEqual(scope.full_validation_reason, "unmapped_changed_paths")

    def test_integrated_rag_paths_select_rag_and_its_dependents(self) -> None:
        for path in (
            "Jenkinsfile.rag",
            "infra/docker/rag-ingestion/pyproject.toml",
            "data_platform/orchestration/airflow/dags/rag_fraud_policy.py",
            "ops/llm_agent/rag_ingest.py",
        ):
            with self.subTest(path=path):
                scope = detect_scope(CATALOG, [path])
                self.assertEqual(set(scope.components), {"rag-pipeline", "mcp-agents", "agents"})
                self.assertIsNone(scope.full_validation_reason)

    def test_flink_and_datahub_paths_have_explicit_owners(self) -> None:
        flink = detect_scope(CATALOG, ["infra/docker/flink-benchmark/Dockerfile"])
        self.assertEqual(flink.components, ("streaming",))
        datahub = detect_scope(CATALOG, ["data_platform/metadata/datahub/pyproject.toml"])
        self.assertEqual(datahub.components, ("data-governance",))

    def test_observability_paths_have_specific_owner(self) -> None:
        for path in (
            "ai_platform/llm_agent/observability.py",
            "scripts/observability/validate.py",
            "tests/observability/test_foundation.py",
            "infra/helm/llm-agent-platform/dashboards/platform-overview.json",
            "infra/helm/llm-agent-platform/templates/otel-collector.yaml",
        ):
            with self.subTest(path=path):
                scope = detect_scope(CATALOG, [path])
                self.assertEqual(scope.components, ("observability",))
                self.assertIsNone(scope.full_validation_reason)

    def test_shared_llm_chart_change_also_validates_observability(self) -> None:
        scope = detect_scope(CATALOG, ["infra/helm/llm-agent-platform/values.yaml"])
        self.assertIn("llm-serving", scope.components)
        self.assertIn("observability", scope.components)

    def test_rubric_component_changes_select_only_their_closure(self) -> None:
        cases = {
            # Row 33: RAG ingestion reaches the MCP retriever and the Specialist that uses it.
            "ai_platform/llm_agent/rag/pipeline.py": {"rag-pipeline", "mcp-agents", "agents"},
            # Row 34: knowledge_search re-checks the MCP server and the Feature-RAG Specialist only.
            "ai_platform/llm_agent/tools/knowledge_search.py": {"mcp-agents", "agents"},
            "ai_platform/llm_agent/byo_coordinator.py": {"agents"},
            "infra/docker/g3-byo-coordinator/Dockerfile": {"agents"},
            # Row 35: a prompt version change re-validates the Agents that load it.
            "infra/helm/llm-agent-platform/prompts/drift-specialist/v2.md": {"prompt-management", "agents"},
            # Row 36: streaming features do not rebuild agents, KServe or MLflow.
            "data_platform/processing/flink/streaming_pipeline.py": {"streaming"},
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                scope = detect_scope(CATALOG, [path])
                self.assertEqual(set(scope.components), expected)
                self.assertIsNone(scope.full_validation_reason)

    def test_full_validation_includes_rag_mcp_agent_and_prompt_components(self) -> None:
        for path in ("jenkins/catalog/components.json", "unknown_runtime/service.py"):
            with self.subTest(path=path):
                scope = detect_scope(CATALOG, [path])
                self.assertTrue(
                    {"rag-pipeline", "mcp-agents", "agents", "prompt-management", "streaming"}
                    <= set(scope.components)
                )

    def test_tool_service_change_fans_out_to_security_and_observability(self) -> None:
        scope = detect_scope(CATALOG, ["ai_platform/llm_agent/tools/service.py"])
        self.assertEqual(set(scope.components), {"mcp-agents", "observability", "agents"})
        self.assertIsNone(scope.full_validation_reason)


class GitScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "CI Test")
        self.git("config", "user.email", "ci@example.invalid")
        self.write("README.md", "base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.git("branch", "target")
        self.git("checkout", "-qb", "feature")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=self.repo, text=True, capture_output=True, check=check
        )

    def write(self, relative: str, content: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, relative: str, content: str, message: str) -> None:
        self.write(relative, content)
        self.git("add", relative)
        self.git("commit", "-qm", message)

    def test_multi_commit_branch_uses_merge_base_range(self) -> None:
        self.commit("ai_platform/inference/server.py", "one\n", "one")
        self.commit("ai_platform/inference/schema.py", "two\n", "two")
        head = self.git("rev-parse", "HEAD").stdout.strip()
        scope = resolve_git_scope(self.repo, CATALOG, head, "target")
        # fraud-api library changes also re-validate the MCP image and agents.
        self.assertEqual(scope.components, ("agents", "fraud-api", "mcp-agents"))
        self.assertEqual(
            scope.changed_files,
            ("ai_platform/inference/schema.py", "ai_platform/inference/server.py"),
        )
        self.assertEqual(scope.merge_base, self.git("rev-parse", "target").stdout.strip())

    def test_missing_target_falls_back_to_full_validation(self) -> None:
        head = self.git("rev-parse", "HEAD").stdout.strip()
        scope = resolve_git_scope(self.repo, CATALOG, head, "missing-target")
        self.assertEqual(scope.components, CATALOG.active_components)
        self.assertEqual(scope.full_validation_reason, "target_ref_unavailable")

    def test_absent_target_falls_back_to_full_validation(self) -> None:
        head = self.git("rev-parse", "HEAD").stdout.strip()
        scope = resolve_git_scope(self.repo, CATALOG, head, None)
        self.assertEqual(scope.components, CATALOG.active_components)
        self.assertEqual(scope.full_validation_reason, "target_ref_not_provided")

    def test_scope_json_has_provenance_fields(self) -> None:
        self.commit("ai_platform/inference/server.py", "one\n", "one")
        head = self.git("rev-parse", "HEAD").stdout.strip()
        data = json.loads(json.dumps(resolve_git_scope(self.repo, CATALOG, head, "target").to_dict()))
        self.assertEqual(data["head"], head)
        self.assertEqual(data["components"], ["agents", "fraud-api", "mcp-agents"])
        self.assertEqual(data["runtime_units"], ["agent_substrate", "coordinator", "drift_specialist", "feature_rag_specialist", "fraud_api", "idempotency_redis", "kagent_platform", "mcp_services"])
        self.assertIsNone(data["full_validation_reason"])


if __name__ == "__main__":
    unittest.main()
