from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from kfp import compiler

TEST_IMAGE = (
    "asia-southeast1-docker.pkg.dev/test-project/ewallet-platform/"
    "f9-kfp-runtime:kfp-2.17.3-f9-v1"
)
CANONICAL_GENERATED_IMAGE = (
    "asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/"
    "ewallet-platform/f9-kfp-runtime:kfp-2.17.3-f9-v1"
)


class KfpCanonicalCompileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(os.environ, {"F9_RUNTIME_IMAGE": TEST_IMAGE}):
            cls.pipeline_module = importlib.import_module(
                "ai_platform.ml.pipelines.pipeline"
            )

    def test_pipeline_compiles_with_frozen_inputs_and_dag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "pipeline.yaml"
            compiler.Compiler().compile(
                pipeline_func=self.pipeline_module.f9_canonical_pipeline,
                package_path=str(output),
            )
            document = yaml.safe_load(output.read_text(encoding="utf-8"))

        parameters = document["root"]["inputDefinitions"]["parameters"]
        for name in (
            "dataset_version",
            "silver_run_id",
            "feature_run_id",
            "snapshot_id",
            "snapshot_uri",
            "snapshot_manifest_sha256",
            "mlflow_tracking_uri",
            "runtime_image",
            "experiment_name",
            "registered_model_name",
            "git_commit_sha",
        ):
            self.assertIn(name, parameters)

        task_names = set(document["root"]["dag"]["tasks"])
        self.assertEqual(
            {
                "validate-snapshot",
                "verify-chronological-splits",
                "train-dummy-baseline",
                "train-logistic-regression",
                "train-random-forest",
                "train-hist-gradient-boosting",
                "select-validation-winner",
                "evaluate-test-winner",
                "publish-winner-to-mlflow",
            },
            task_names,
        )
        self.assertEqual(
            (
                "dummy_baseline",
                "logistic_regression",
                "random_forest",
                "hist_gradient_boosting",
            ),
            self.pipeline_module.CANDIDATE_NAMES,
        )
        self.assertNotIn(":latest", TEST_IMAGE)
        self.assertEqual("2.17.3", self.pipeline_module.KFP_VERSION)

    def test_compiled_components_have_bounded_resources_and_pinned_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "pipeline.yaml"
            compiler.Compiler().compile(
                pipeline_func=self.pipeline_module.f9_canonical_pipeline,
                package_path=str(output),
            )
            rendered = output.read_text(encoding="utf-8")

        self.assertIn(TEST_IMAGE, rendered)
        self.assertIn("cpuLimit", rendered)
        self.assertIn("memoryLimit", rendered)
        self.assertNotIn("nvidia.com/gpu", rendered)
        self.assertNotIn("minio", rendered.lower())

    def test_checked_in_versions_and_image_are_reproducibly_pinned(self) -> None:
        root = Path(__file__).resolve().parents[2]
        versions = (root / "infra/kubernetes/kfp/versions.env").read_text()
        package = (root / "ai_platform/ml/pipelines/pyproject.toml").read_text()
        dockerfile = (
            root / "infra/kubernetes/kfp/image/Dockerfile"
        ).read_text()

        self.assertIn("KFP_VERSION=2.17.3", versions)
        self.assertIn("KFP_SDK_VERSION=2.17.3", versions)
        self.assertIn('kfp==2.17.3', package)
        self.assertIn("FROM python:3.10.19-slim-bookworm", dockerfile)
        self.assertNotIn(":latest", versions + dockerfile)

    def test_user_generated_pipeline_preserves_canonical_contract(self) -> None:
        root = Path("ai_platform/ml/pipelines/compiled")
        artifact = root / "f9_canonical_pipeline.yaml"
        self.assertTrue(artifact.is_file())
        document = yaml.safe_load(artifact.read_text(encoding="utf-8"))
        executors = document["deploymentSpec"]["executors"]
        expected_trainers = {
            "exec-train-dummy-baseline",
            "exec-train-logistic-regression",
            "exec-train-random-forest",
            "exec-train-hist-gradient-boosting",
        }
        self.assertTrue(expected_trainers <= set(executors))
        for executor in executors.values():
            container = executor["container"]
            self.assertEqual(CANONICAL_GENERATED_IMAGE, container["image"])
            self.assertNotIn(":latest", container["image"])
            resources = container["resources"]
            for field in (
                "resourceCpuRequest",
                "resourceCpuLimit",
                "resourceMemoryRequest",
                "resourceMemoryLimit",
            ):
                self.assertIn(field, resources)
        for name in expected_trainers:
            container = executors[name]["container"]
            self.assertEqual("2", container["resources"]["resourceCpuLimit"])
            self.assertEqual("3Gi", container["resources"]["resourceMemoryLimit"])
            self.assertIn("--runtime-image", container["args"])
            self.assertIn("--compiled-runtime-image", container["args"])
            compiled_index = container["args"].index("--compiled-runtime-image")
            self.assertEqual(
                CANONICAL_GENERATED_IMAGE,
                container["args"][compiled_index + 1],
            )

        status = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn("USER_GENERATED", status)
        self.assertIn(
            "4f8c039f76123519828702aea07765e5d882c453d98aab0aa408bd2dfebcf4b4",
            status,
        )


if __name__ == "__main__":
    unittest.main()
