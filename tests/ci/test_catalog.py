from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.ci.catalog import CatalogError, load_catalog


ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "jenkins/catalog/components.json"
EXPECTED_PROJECTS = {
    "ai_platform/features/feast",
    "ai_platform/inference",
    "ai_platform/ml",
    "ai_platform/ml/dvc-operator",
    "ai_platform/ml/pipelines",
    "ai_platform/serving",
    "infra/docker/llm-agent-tooling",
    "infra/docker/vllm-cpu-model",
    "infra/kubernetes/mlflow/image",
}


class RepositoryCatalogTests(unittest.TestCase):
    def test_catalog_covers_all_nine_locked_python_projects(self) -> None:
        catalog = load_catalog(CATALOG)
        catalog.validate(ROOT)
        self.assertEqual(set(catalog.python_projects), EXPECTED_PROJECTS)

    def test_catalog_references_existing_paths_and_commands(self) -> None:
        catalog = load_catalog(CATALOG)
        catalog.validate(ROOT)
        self.assertGreaterEqual(len(catalog.active_components), 8)
        self.assertIn("python3", " ".join(
            command
            for component in catalog.components.values()
            for command in component.commands
        ))
        matrix = catalog.matrix_markdown()
        self.assertIn("| Component | Source paths | Dependencies | Tests | Build artifact | Deployment target |", matrix)
        self.assertIn("ai_platform/inference", matrix)
        self.assertIn("ai_platform/serving", matrix)


class InvalidCatalogTests(unittest.TestCase):
    def write_catalog(self, data: dict) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "components.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def base(self) -> dict:
        return {
            "schema_version": 1,
            "shared_paths": [],
            "python_projects": [],
            "legacy_path_components": {"tests/ci/": ["legacy-ci"]},
            "profiles": {"all": ["one"]},
            "components": {
                "one": {
                    "maturity": "active",
                    "source_paths": ["tests/ci/"],
                    "dependencies": [],
                    "python_projects": [],
                    "commands": ["python3 -m unittest tests.ci.test_catalog"],
                    "build_artifact": None,
                    "deployment_target": None,
                }
            },
        }

    def test_rejects_unknown_component_dependency(self) -> None:
        data = self.base()
        data["components"]["one"]["dependencies"] = ["missing"]
        with self.assertRaisesRegex(CatalogError, "unknown dependency"):
            load_catalog(self.write_catalog(data)).validate(ROOT)

    def test_rejects_dependency_cycle(self) -> None:
        data = self.base()
        data["components"]["two"] = {**data["components"]["one"], "dependencies": ["one"]}
        data["components"]["one"]["dependencies"] = ["two"]
        with self.assertRaisesRegex(CatalogError, "cycle"):
            load_catalog(self.write_catalog(data)).validate(ROOT)

    def test_rejects_python_project_without_lock(self) -> None:
        data = self.base()
        data["python_projects"] = ["tests/ci"]
        with self.assertRaisesRegex(CatalogError, "pyproject.toml"):
            load_catalog(self.write_catalog(data)).validate(ROOT)

    def test_rejects_extension_only_deployment_target(self) -> None:
        data = self.base()
        data["components"]["one"].update(
            maturity="extension-only", deployment_target="cluster/example"
        )
        with self.assertRaisesRegex(CatalogError, "extension-only"):
            load_catalog(self.write_catalog(data)).validate(ROOT)


if __name__ == "__main__":
    unittest.main()
