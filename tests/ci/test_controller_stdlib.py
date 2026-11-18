"""The Jenkins controller runs scripts/ci CLIs with its system python3, which has no PyYAML.

Batch 2B-1 found this on the real controller: catalog validation reached sandbox_agents, whose
top-level ``import yaml`` failed, and every job stopped at "Validate catalog and detect scope".
These tests run the controller's real code paths in a subprocess where ``import yaml`` is blocked.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BLOCK = """
import importlib.abc, runpy, sys
class NoYaml(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "yaml" or name.startswith("yaml."):
            raise ImportError("PyYAML is not installed on the Jenkins controller")
sys.meta_path.insert(0, NoYaml())
module, sys.argv = sys.argv[1], sys.argv[1:]
runpy.run_module(module, run_name="__main__", alter_sys=True)
"""


def controller(module: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", BLOCK, module, *args], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)})


def test_catalog_validation_runs_without_pyyaml() -> None:
    result = controller("scripts.ci.catalog", "validate")
    assert result.returncode == 0, result.stderr[-800:]
    assert json.loads(result.stdout)["status"] == "PASS"


@pytest.mark.parametrize("module", ["scripts.ci.sandbox_agents", "scripts.ci.k8s_api", "scripts.ci.release_lock",
                                    "scripts.ci.deploy_identity", "scripts.ci.release_units"])
def test_indirectly_imported_modules_import_without_pyyaml(module) -> None:
    code = BLOCK.replace("module, sys.argv = sys.argv[1], sys.argv[1:]\nrunpy.run_module(module, run_name=\"__main__\", alter_sys=True)",
                         f"import {module}")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)})
    assert result.returncode == 0, result.stderr[-800:]


def test_release_policy_and_handoff_run_without_pyyaml(tmp_path) -> None:
    from tests.ci.test_release_safety import handoff_sources

    sources = handoff_sources(tmp_path)
    plan = json.loads(sources["release-plan.json"].read_text())
    verified = controller("scripts.ci.release_policy", "--root", str(ROOT), "verify", "--plan",
                          str(sources["release-plan.json"]))
    assert verified.returncode == 0, verified.stderr[-800:]
    args = ["build", "--out", str(tmp_path / "handoff")]
    for name, path in sources.items():
        args += [f"--{name.split('.')[0]}", str(path)]
    built = controller("scripts.ci.release_handoff", *args)
    assert built.returncode == 0, built.stderr[-800:]
    assert built.stdout.startswith("RELEASE_HANDOFF sha256=") and plan["stage"] == "production"


def test_change_detection_runs_without_pyyaml(tmp_path) -> None:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    result = controller("scripts.ci.change_detection", "--root", str(ROOT), "--head", head, "--target-ref", "HEAD~1",
                        "--output", str(tmp_path / "scope.json"))
    assert result.returncode == 0, result.stderr[-800:]
