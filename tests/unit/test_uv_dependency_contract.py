"""uv dependency contract: pyproject.toml + uv.lock are authoritative.

- every Python project directory carries both files, and `uv lock --check`
  passes when uv is installed;
- legacy requirements mirrors equal the locked versions of the direct pins;
- every image that installs Python dependencies does so with `uv sync --locked`
  (never `pip install -r`), and Jenkins runs tests with `uv run --locked`;
- protected pins (Feast, KFP, MCP SDK, DVC operator) are not upgraded silently.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

try:  # Python 3.11+; the 3.10 runtimes skip this contract test
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

ROOT = Path(__file__).resolve().parents[2]
PROJECTS = (
    "ai_platform/features/feast",
    "ai_platform/ml",
    "ai_platform/ml/pipelines",
    "ai_platform/ml/dvc-operator",
    "infra/docker/flink-benchmark",
    "infra/docker/llm-agent-tooling",
    "infra/docker/vllm-cpu-model",
    "infra/kubernetes/mlflow/image",
)
MIRRORS = {
    "ai_platform/features/feast/requirements.txt": "ai_platform/features/feast",
    "ai_platform/ml/requirements.txt": "ai_platform/ml",
    "ai_platform/ml/pipelines/requirements-kfp.txt": "ai_platform/ml/pipelines",
    "ai_platform/ml/dvc-requirements.txt": "ai_platform/ml/dvc-operator",
}
PROTECTED = {
    ("ai_platform/features/feast", "feast"): "0.66.0",
    ("infra/docker/llm-agent-tooling", "feast"): "0.66.0",
    ("ai_platform/ml/pipelines", "kfp"): "2.17.3",
    ("infra/docker/llm-agent-tooling", "mcp"): "2.3.0",
    ("ai_platform/ml/dvc-operator", "dvc"): "3.63.0",
    # PyFlink must match the pinned Flink 1.19.1 JobManager/TaskManager image.
    ("infra/docker/flink-benchmark", "apache-flink"): "1.19.1",
    # vllm-cpu-model remediation inputs (Trivy fixes); changing them needs a rescan.
    ("infra/docker/vllm-cpu-model", "certifi"): "2026.10.18",
    ("infra/docker/vllm-cpu-model", "setuptools"): "82.0.1",
}
UV = shutil.which("uv")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", re.sub(r"\[.*\]", "", name)).lower()


def locked(project: str) -> dict[str, str]:
    lock = tomllib.loads((ROOT / project / "uv.lock").read_text(encoding="utf-8"))
    return {norm(p["name"]): p["version"] for p in lock["package"] if "version" in p}


def pins(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*==\s*([^\s;#,]+)", line)
        if match:
            result[norm(match.group(1))] = match.group(2)
    return result


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
class UvDependencyContractTests(unittest.TestCase):
    def test_every_project_is_locked(self):
        found = {str(p.parent.relative_to(ROOT)) for p in ROOT.rglob("pyproject.toml")
                 if ".venv" not in p.parts and "node_modules" not in p.parts and p.parent != ROOT}
        self.assertTrue(found <= set(PROJECTS), sorted(found - set(PROJECTS)))
        for project in PROJECTS:
            with self.subTest(project):
                self.assertTrue((ROOT / project / "uv.lock").is_file())

    @unittest.skipUnless(UV, "uv not installed")
    def test_locks_match_their_pyproject(self):
        for project in PROJECTS:
            with self.subTest(project):
                proc = subprocess.run([UV, "lock", "--check", "--offline"], cwd=ROOT / project,
                                      capture_output=True, text=True, check=False, timeout=120)
                self.assertEqual(proc.returncode, 0, proc.stderr[-400:])

    def test_requirements_mirrors_equal_the_lock(self):
        for mirror, project in MIRRORS.items():
            with self.subTest(mirror):
                text = (ROOT / mirror).read_text(encoding="utf-8")
                self.assertTrue(text.lstrip().startswith("#"), "mirror must say where it comes from")
                direct = pins(" ".join(tomllib.loads((ROOT / project / "pyproject.toml").read_text())
                                       ["project"]["dependencies"]).replace(" ", "\n"))
                versions = locked(project)
                for name, version in pins(text).items():
                    self.assertEqual(versions.get(name), version, name)
                    self.assertEqual(direct.get(name), version, name)
                self.assertEqual(set(pins(text)), set(direct))

    def test_images_install_only_from_the_lock(self):
        dockerfiles = [p for p in ROOT.rglob("Dockerfile") if ".venv" not in p.parts]
        self.assertTrue(dockerfiles)
        for dockerfile in dockerfiles:
            text = dockerfile.read_text(encoding="utf-8")
            with self.subTest(str(dockerfile.relative_to(ROOT))):
                self.assertNotRegex(text, r"pip3? install\s+(-r|--requirement)")
                for line in re.findall(r"uv (?:sync|run)[^\n]*", text):
                    self.assertIn("--locked", line)
                    self.assertNotIn("--frozen", line)

    def test_jenkins_runs_against_the_lock(self):
        text = (ROOT / "Jenkinsfile.llm-agent").read_text(encoding="utf-8")
        for line in re.findall(r"uv (?:sync|run) [^\n]*", text):
            self.assertIn("--locked", line)

    def test_protected_pins_are_not_upgraded(self):
        for (project, name), version in PROTECTED.items():
            with self.subTest(project=project, package=name):
                self.assertEqual(locked(project).get(name), version)

    def test_dvc_operator_matches_the_guard(self):
        guard = (ROOT / "ops/gcp/ml2_operator_guard.py").read_text(encoding="utf-8")
        pinned = re.search(r'PINNED_DVC_VERSION = "([^"]+)"', guard).group(1)
        self.assertEqual(locked("ai_platform/ml/dvc-operator")["dvc"], pinned)
        # DVC stays out of the F9 runtime and KFP images.
        for project in ("ai_platform/ml", "ai_platform/ml/pipelines"):
            self.assertNotIn("dvc", locked(project))


if __name__ == "__main__":
    unittest.main()
