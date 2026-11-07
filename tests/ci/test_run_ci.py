from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.ci.catalog import Catalog, Component
from scripts.ci.run_ci import execute_components, worktree_metadata


class RecordingExecutor:
    def __init__(self, exit_codes: list[int] | None = None) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.exit_codes = list(exit_codes or [])

    def __call__(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, cwd))
        code = self.exit_codes.pop(0) if self.exit_codes else 0
        return subprocess.CompletedProcess(argv, code, stdout="out\n", stderr="err\n" if code else "")


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for project in ("project-a", "project-b"):
            directory = self.root / project
            directory.mkdir()
            (directory / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
            (directory / "uv.lock").write_text(f"# {project}\n")
        self.catalog = Catalog(
            schema_version=1,
            shared_paths=("scripts/ci/",),
            python_projects=("project-a", "project-b"),
            legacy_path_components={"legacy/": ("legacy",)},
            profiles={"all": ("one",)},
            runtime_units={"one-runtime": "one"},
            components={
                "one": Component(
                    name="one",
                    maturity="active",
                    source_paths=("project-a",),
                    dependencies=(),
                    python_projects=("project-a", "project-b"),
                    commands=('python3 -c "print(1)"',),
                    build_artifact=None,
                    deployment_target=None,
                    deploy_handler=None,
                )
            },
        )

    def test_uv_lock_then_sync_run_in_each_project_before_tests(self) -> None:
        executor = RecordingExecutor()
        outcome = execute_components(
            self.catalog,
            ("one",),
            self.root,
            self.root / "evidence",
            executor=executor,
            tool_lookup=lambda name: f"/tools/{name}",
            metadata={"git_sha": "c" * 40},
        )
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(
            executor.calls,
            [
                (["/tools/uv", "lock", "--check"], self.root / "project-a"),
                (["/tools/uv", "sync", "--locked"], self.root / "project-a"),
                (["/tools/uv", "lock", "--check"], self.root / "project-b"),
                (["/tools/uv", "sync", "--locked"], self.root / "project-b"),
                (["/tools/python3", "-c", "print(1)"], self.root),
            ],
        )

    def test_missing_tool_fails_and_still_finalizes_evidence(self) -> None:
        nested = self.root / "nested/project-a"
        nested.mkdir(parents=True)
        (nested / "pyproject.toml").write_text("[project]\nname='nested'\nversion='0'\n")
        (nested / "uv.lock").write_text("# nested\n")
        component = Component(
            **{**self.catalog.components["one"].__dict__, "python_projects": ("nested/project-a",)}
        )
        catalog = Catalog(
            **{**self.catalog.__dict__, "python_projects": ("nested/project-a",),
               "components": {"one": component}}
        )
        executor = RecordingExecutor()
        outcome = execute_components(
            catalog,
            ("one",),
            self.root,
            self.root / "missing-evidence",
            executor=executor,
            tool_lookup=lambda name: None if name == "uv" else f"/tools/{name}",
            metadata={"git_sha": "d" * 40},
        )
        self.assertEqual(outcome.exit_code, 127)
        self.assertEqual(outcome.results[-1]["classification"], "MISSING_RUNTIME_DEPENDENCY")
        self.assertEqual(
            outcome.results[-1]["log"],
            "logs/0001-uv-lock-nested-project-a.log",
        )
        self.assertTrue((self.root / "missing-evidence/manifest.json").is_file())
        self.assertTrue((self.root / "missing-evidence/SHA256SUMS").is_file())
        self.assertEqual(executor.calls, [])

    def test_nonzero_command_exit_propagates_without_running_later_steps(self) -> None:
        executor = RecordingExecutor([0, 0, 9])
        catalog = Catalog(
            **{**self.catalog.__dict__, "components": {
                "one": Component(
                    **{**self.catalog.components["one"].__dict__, "python_projects": ("project-a",)}
                )
            }}
        )
        outcome = execute_components(
            catalog,
            ("one",),
            self.root,
            self.root / "failed-evidence",
            executor=executor,
            tool_lookup=lambda name: f"/tools/{name}",
            metadata={},
        )
        self.assertEqual(outcome.exit_code, 9)
        self.assertEqual(len(executor.calls), 3)
        self.assertEqual(outcome.results[-1]["classification"], "SOURCE_REGRESSION")

    def test_unittest_commands_write_one_junit_report_per_stage(self) -> None:
        executor = RecordingExecutor()
        component = Component(
            **{**self.catalog.components["one"].__dict__, "python_projects": (),
               "commands": (
                   "uv run --locked --project project-a python -m unittest tests.a tests.b",
                   'python3 -c "print(1)"',
               )}
        )
        catalog = Catalog(**{**self.catalog.__dict__, "components": {"one": component}})
        evidence = self.root / "junit-evidence"
        execute_components(
            catalog, ("one",), self.root, evidence, executor=executor,
            tool_lookup=lambda name: f"/tools/{name}", metadata={},
        )
        self.assertEqual(
            executor.calls[0][0],
            ["/tools/uv", "run", "--locked", "--project", "project-a", "python", "-m",
             "scripts.ci.junit_unittest", "--junit-xml",
             str(evidence / "junit/junit-0001-component-one-1.xml"),
             "--suite-name", "component:one:1", "tests.a", "tests.b"],
        )
        self.assertEqual(executor.calls[1][0], ["/tools/python3", "-c", "print(1)"])

    def test_pytest_commands_write_one_junit_report_per_stage(self) -> None:
        executor = RecordingExecutor()
        component = Component(
            **{**self.catalog.components["one"].__dict__, "python_projects": (),
               "commands": ("uv run --locked --project project-a --group test python -m pytest -q tests/a",)}
        )
        catalog = Catalog(**{**self.catalog.__dict__, "components": {"one": component}})
        evidence = self.root / "junit-evidence"
        execute_components(
            catalog, ("one",), self.root, evidence, executor=executor,
            tool_lookup=lambda name: f"/tools/{name}", metadata={},
        )
        self.assertEqual(
            executor.calls[0][0],
            ["/tools/uv", "run", "--locked", "--project", "project-a", "--group", "test", "python", "-m",
             "pytest", f"--junitxml={evidence / 'junit/junit-0001-component-one-1.xml'}",
             "-o", "junit_suite_name=component:one:1", "-q", "tests/a"],
        )

    def test_commands_are_argument_lists_not_shell_programs(self) -> None:
        executor = RecordingExecutor()
        component = Component(
            **{**self.catalog.components["one"].__dict__, "python_projects": (),
               "commands": ('python3 -c "print(1)" ; touch SHOULD_NOT_EXIST',)}
        )
        catalog = Catalog(**{**self.catalog.__dict__, "components": {"one": component}})
        execute_components(
            catalog,
            ("one",),
            self.root,
            self.root / "argv-evidence",
            executor=executor,
            tool_lookup=lambda name: f"/tools/{name}",
            metadata={},
        )
        self.assertEqual(
            executor.calls[0][0],
            ["/tools/python3", "-c", "print(1)", ";", "touch", "SHOULD_NOT_EXIST"],
        )
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())

    def test_worktree_metadata_identifies_dirty_source(self) -> None:
        repository = self.root / "metadata-repo"
        repository.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "CI Test"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "ci@example.invalid"], cwd=repository, check=True)
        tracked = repository / "tracked.txt"
        tracked.write_text("clean\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
        self.assertEqual(worktree_metadata(repository), {"working_tree_dirty": False, "working_tree_status_sha256": None})
        tracked.write_text("dirty\n")
        state = worktree_metadata(repository)
        self.assertTrue(state["working_tree_dirty"])
        self.assertRegex(state["working_tree_status_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
