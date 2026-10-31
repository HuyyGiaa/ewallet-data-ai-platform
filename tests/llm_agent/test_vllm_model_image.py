"""vllm-cpu-model image recipe: locked inputs and recorded deviations.

The upstream vLLM venv is not re-resolved; the remediation installs only this
project's locked export (hash-checked, no deps), upgrades one pinned Ubuntu
package and swaps two vendored packages of setuptools 78.1.1 (pinned by vLLM)
with those of the locked setuptools wheel. These tests pin each deviation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "infra/docker/vllm-cpu-model"
DOCKERFILE = (RECIPE / "Dockerfile").read_text()
UV_IMAGE = "ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff"


def remediate():
    spec = importlib.util.spec_from_file_location("remediate", RECIPE / "remediate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecipeTests(unittest.TestCase):
    def test_base_is_the_chart_image_and_uv_is_pinned(self):
        base = yaml.safe_load((ROOT / "infra/helm/llm-agent-platform/values.yaml").read_text())["vllm"]["image"]
        self.assertIn(f"FROM {base} AS remediated\n", DOCKERFILE)
        self.assertIn(f"COPY --from={UV_IMAGE} /uv", DOCKERFILE)
        # Same uv as the other locked image.
        self.assertIn(UV_IMAGE.split("@")[1], (ROOT / "infra/docker/llm-agent-tooling/Dockerfile").read_text())

    def test_python_changes_come_only_from_the_lock(self):
        run = re.search(r"RUN cd /tmp/remediation(.*?)\n\n", DOCKERFILE, re.S).group(1)
        steps = [s.strip().strip("\\").strip() for s in run.split("&&")]
        steps = [s for s in steps if s]
        self.assertTrue(steps[0].startswith("/tmp/uv-pinned/uv lock --check"))
        self.assertTrue(steps[1].startswith("/tmp/uv-pinned/uv export --locked"))
        self.assertRegex(steps[2], r"uv pip install --python /opt/venv/bin/python3 --require-hashes --no-deps -r requirements\.txt")
        self.assertTrue(steps[3].startswith("/opt/venv/bin/python3 remediate.py uv.lock"))
        self.assertNotRegex(DOCKERFILE, r"(?<!uv )pip3? install")
        self.assertNotIn("--frozen", DOCKERFILE)

    def test_os_deviation_is_one_pinned_upgrade(self):
        apt = re.findall(r"apt-get install[^\n]*", DOCKERFILE)
        self.assertEqual(apt, ["apt-get install -y --no-install-recommends --only-upgrade libbrotli1=1.0.9-2ubuntu0.1 \\"])

    def test_deviations_are_recorded_in_the_dockerfile(self):
        for phrase in ("not re-resolved", "libbrotli1", "setuptools 78.1.1", "pip is removed"):
            self.assertIn(phrase, DOCKERFILE)

    def test_vendored_swap_comes_from_the_locked_wheel(self):
        module = remediate()
        url, sha256 = module.locked_wheel(str(RECIPE / "uv.lock"), "setuptools")
        self.assertTrue(url.endswith("/setuptools-82.0.1-py3-none-any.whl"))
        self.assertRegex(sha256, r"^[0-9a-f]{64}$")
        new = {prefix.split("/")[2] for prefix in module.VENDORED}
        self.assertTrue({"jaraco_context-6.1.0.dist-info", "wheel-0.46.3.dist-info"} <= new)
        self.assertEqual(module.EXPECTED, {"certifi": "2026.10.18", "setuptools": "78.1.1"})
        source = (RECIPE / "remediate.py").read_text()
        for check in ("build_meta.build_wheel", "jaraco.text", "torch.utils.cpp_extension", '"vllm"'):
            self.assertIn(check, source)

    def test_final_stage_serves_the_checksummed_revision_offline(self):
        final = DOCKERFILE.split("FROM remediated", 1)[1]
        self.assertIn("ARG MODEL_DIR=/models/Qwen3-0.6B/c1899de289a04d12100db370d81485cdf75e47ca", final)
        self.assertIn("sha256sum --check --strict --quiet SHA256SUMS", final)
        self.assertIn("ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1", final)
        gke = yaml.safe_load((ROOT / "infra/helm/llm-agent-platform/values-gke-cpu.yaml").read_text())
        self.assertEqual(gke["vllm"]["modelSource"]["image"]["path"],
                         "/models/Qwen3-0.6B/c1899de289a04d12100db370d81485cdf75e47ca")


class BuildScriptTests(unittest.TestCase):
    def test_uv_contract_runs_before_docker_build(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            log = folder / "calls.log"
            bin_dir = folder / "bin"
            bin_dir.mkdir()
            for name in ("uv", "docker"):
                fake = bin_dir / name
                fake.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{log}"\n')
                fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            model = folder / "model"
            for line in (RECIPE / "SHA256SUMS").read_text().splitlines():
                (model / line.split("  ", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
            # Real checksums cannot match fixture files: the script must stop before uv/docker.
            for line in (RECIPE / "SHA256SUMS").read_text().splitlines():
                (model / line.split("  ", 1)[1]).write_text("x")
            proc = subprocess.run([str(ROOT / "scripts/llm_agent/build_vllm_model_image.sh"), str(model), "r/x",
                                   "0" * 40, str(folder / "d.json")], capture_output=True, text=True,
                                  env={"PATH": f"{bin_dir}:/usr/bin:/bin", "UV": str(bin_dir / "uv")}, check=False)
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(log.exists())
        text = (ROOT / "scripts/llm_agent/build_vllm_model_image.sh").read_text()
        order = [text.index(s) for s in ('"$uv" lock --check', '"$uv" sync --locked', '"$uv" run --locked', "docker build")]
        self.assertEqual(order, sorted(order))


if __name__ == "__main__":
    unittest.main()
