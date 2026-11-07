from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
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
    "data_platform/generation",
    "data_platform/metadata/datahub",
    "infra/docker/flink-benchmark",
    "infra/docker/g3-byo-coordinator",
    "infra/docker/llm-agent-tooling",
    "infra/docker/rag-ingestion",
    "infra/docker/streaming-sinks",
    "infra/docker/vllm-cpu-model",
    "infra/kubernetes/mlflow/image",
    "infra/load-test",
}


class RepositoryCatalogTests(unittest.TestCase):
    def test_catalog_declares_every_owner_approved_runtime_unit(self) -> None:
        catalog = load_catalog(CATALOG)
        self.assertEqual(
            set(catalog.runtime_units),
            {
                "ml_training", "kfp_pipeline", "mlflow", "feast", "kserve",
                "fraud_api", "rag_ingestion", "llm_serving", "agentgateway",
                "kagent_platform", "coordinator", "feature_rag_specialist",
                "drift_specialist", "mcp_services", "idempotency_redis",
                "agent_substrate", "observability", "ci_config",
                "integration_contracts", "prompt_registry",
            },
        )
        catalog.validate(ROOT)
    def test_feast_dependent_tool_tests_run_with_the_feast_extra(self) -> None:
        # test_phase_d_tools patches feast.FeatureStore (the reviewed KServe wire
        # contract test); every scope that runs it needs the locked feast extra.
        catalog = load_catalog(CATALOG)
        commands = [
            command
            for component in catalog.components.values()
            for command in component.commands
            if "tests.llm_agent.test_phase_d_tools" in command
        ]
        self.assertGreaterEqual(len(commands), 2)
        for command in commands:
            with self.subTest(command=command):
                self.assertIn("--project infra/docker/llm-agent-tooling --extra feast", command)

    def test_catalog_covers_all_locked_python_projects(self) -> None:
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
        self.assertEqual(
            catalog.components["streaming"].python_projects,
            ("infra/docker/flink-benchmark", "infra/docker/streaming-sinks"),
        )
        self.assertEqual(
            catalog.components["data-governance"].python_projects,
            ("data_platform/metadata/datahub",),
        )
        self.assertIn(
            "infra/docker/rag-ingestion",
            catalog.components["rag-pipeline"].python_projects,
        )
        self.assertEqual(
            catalog.components["observability"].python_projects,
            ("infra/docker/llm-agent-tooling",),
        )
        self.assertIn("observability", catalog.profiles["platform"])
        self.assertIn("observability", catalog.profiles["llm-agent"])


def _locked_packages(project: str) -> set[str]:
    lock = (ROOT / project / "uv.lock").read_text(encoding="utf-8")
    return set(re.findall(r'^name = "([^"]+)"$', lock, re.MULTILINE))


def _project_of(command: str) -> str | None:
    argv = shlex.split(command)
    return argv[argv.index("--project") + 1] if "--project" in argv else None


class CommandEnvironmentTests(unittest.TestCase):
    """Component commands run in locked project environments, not the Jenkins system python.

    The Jenkins controller's /usr/bin/python3 has no third-party packages, so a bare
    ``python3`` command only works for stdlib-only tests (N9 dry-run 2026-11-04).
    """

    # rag-pipeline#2: tests.llm_agent.test_phase_c is stdlib-only.
    BARE_PYTHON3_ALLOWED = {
        ("rag-pipeline", "python3 -m unittest tests.llm_agent.test_phase_c"),
    }

    def setUp(self) -> None:
        self.catalog = load_catalog(CATALOG)

    def test_bare_python3_commands_are_explicitly_allowed(self) -> None:
        bare = {
            (name, command)
            for name, component in self.catalog.components.items()
            for command in component.commands
            if shlex.split(command)[0] == "python3"
        }
        self.assertEqual(bare, self.BARE_PYTHON3_ALLOWED)

    def test_project_commands_use_a_listed_locked_project(self) -> None:
        for name, component in self.catalog.components.items():
            for command in component.commands:
                project = _project_of(command)
                if project is None:
                    continue
                with self.subTest(component=name, command=command):
                    self.assertIn("--locked", shlex.split(command))
                    self.assertIn(project, component.python_projects)
                    self.assertTrue((ROOT / project / "uv.lock").is_file())

    def test_yaml_dependent_tests_run_where_pyyaml_is_locked(self) -> None:
        modules = {
            "mlflow": "tests.unit.test_deploy_mlflow_script",
            "llm-serving": "tests.llm_agent.test_gke_cpu_profile",
            "llm-d-routing": "tests.llm_agent.test_llmd_routing",
        }
        for name, module in modules.items():
            command = next(c for c in self.catalog.components[name].commands if module in c)
            with self.subTest(component=name):
                project = _project_of(command)
                self.assertIsNotNone(project)
                self.assertIn("pyyaml", _locked_packages(project))

    def test_data_platform_tests_run_in_the_generator_project(self) -> None:
        # Faker is a runtime dependency of the source generator; pytest-style modules
        # must run under pytest (unittest collects zero tests from them).
        commands = self.catalog.components["data-platform"].commands
        self.assertTrue(all(_project_of(c) == "data_platform/generation" for c in commands))
        self.assertIn("faker", _locked_packages("data_platform/generation"))
        pytest_command = next(c for c in commands if "-m pytest" in c)
        self.assertIn("tests/unit/test_schema_inventory.py", pytest_command)
        self.assertNotIn("test_schema_inventory", " ".join(c for c in commands if "-m unittest" in c))

    def test_streaming_runs_with_the_locked_flink_extra(self) -> None:
        # Without pyflink, test_source_wiring_contract is skipped as NOT_EXECUTED.
        (command,) = self.catalog.components["streaming"].commands
        argv = shlex.split(command)
        self.assertEqual(_project_of(command), "infra/docker/flink-benchmark")
        self.assertEqual(argv[argv.index("--extra") + 1], "flink")
        self.assertIn("apache-flink", _locked_packages("infra/docker/flink-benchmark"))


class InvalidCatalogTests(unittest.TestCase):
    def write_catalog(self, data: dict) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "components.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def write_raw_catalog(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "components.json"
        path.write_text(text, encoding="utf-8")
        return path

    def base(self) -> dict:
        return {
            "schema_version": 1,
            "shared_paths": [],
            "python_projects": [],
            "legacy_path_components": {"tests/ci/": ["legacy-ci"]},
            "profiles": {"all": ["one"]},
            "runtime_units": {"one-runtime": "one"},
            "components": {
                "one": {
                    "maturity": "active",
                    "source_paths": ["tests/ci/"],
                    "dependencies": [],
                    "python_projects": [],
                    "commands": ["python3 -m unittest tests.ci.test_catalog"],
                    "build_artifact": None,
                    "deployment_target": None,
                    "deploy_handler": None,
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

    def test_rejects_missing_build_artifact(self) -> None:
        data = self.base()
        data["components"]["one"]["build_artifact"] = "missing/Dockerfile"
        with self.assertRaisesRegex(CatalogError, "build artifact does not exist"):
            load_catalog(self.write_catalog(data)).validate(ROOT)

    def test_rejects_duplicate_component_identifier(self) -> None:
        text = json.dumps(self.base())
        text = text.replace('"components": {"one":', '"components": {"one": {}, "one":', 1)
        with self.assertRaisesRegex(CatalogError, "duplicate JSON key: one"):
            load_catalog(self.write_raw_catalog(text))


if __name__ == "__main__":
    unittest.main()
