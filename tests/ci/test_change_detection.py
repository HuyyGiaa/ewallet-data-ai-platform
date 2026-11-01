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
        scope = detect_scope(CATALOG, ["ai_platform/inference/server.py"])
        self.assertEqual(scope.components, ("fraud-api",))
        self.assertIsNone(scope.full_validation_reason)

    def test_upstream_change_adds_reverse_dependency_closure(self) -> None:
        scope = detect_scope(CATALOG, ["ai_platform/features/feast/schema.py"])
        self.assertEqual(
            set(scope.components),
            {"feast", "ml-training", "mlflow", "fraud-serving", "fraud-api", "rag-pipeline", "mcp-agents"},
        )
        self.assertNotIn("streaming", scope.components)

    def test_shared_path_selects_every_active_component(self) -> None:
        scope = detect_scope(CATALOG, ["scripts/ci/catalog.py"])
        self.assertEqual(scope.components, CATALOG.active_components)
        self.assertEqual(scope.full_validation_reason, "shared_path_changed")

    def test_unmapped_path_falls_back_to_every_active_component(self) -> None:
        scope = detect_scope(CATALOG, ["README.md"])
        self.assertEqual(scope.components, CATALOG.active_components)
        self.assertEqual(scope.full_validation_reason, "unmapped_changed_paths")


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
        self.assertEqual(scope.components, ("fraud-api",))
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
        self.assertEqual(data["components"], ["fraud-api"])
        self.assertIsNone(data["full_validation_reason"])


if __name__ == "__main__":
    unittest.main()
