from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml
from kfp import compiler

from ai_platform.ml.pipelines.pipeline import (
    CANDIDATE_NAMES,
    F9_RUNTIME_IMAGE,
    KFP_VERSION,
    f9_canonical_pipeline,
)


class KfpCanonicalCompileTest(unittest.TestCase):
    def test_pipeline_compiles_with_frozen_inputs_and_dag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "pipeline.yaml"
            compiler.Compiler().compile(
                pipeline_func=f9_canonical_pipeline,
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
            CANDIDATE_NAMES,
        )
        self.assertNotIn(":latest", F9_RUNTIME_IMAGE)
        self.assertEqual("2.17.3", KFP_VERSION)

    def test_compiled_components_have_bounded_resources_and_pinned_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "pipeline.yaml"
            compiler.Compiler().compile(
                pipeline_func=f9_canonical_pipeline,
                package_path=str(output),
            )
            rendered = output.read_text(encoding="utf-8")

        self.assertIn(F9_RUNTIME_IMAGE, rendered)
        self.assertIn("cpuLimit", rendered)
        self.assertIn("memoryLimit", rendered)
        self.assertNotIn("nvidia.com/gpu", rendered)
        self.assertNotIn("minio", rendered.lower())

    def test_checked_in_versions_and_image_are_reproducibly_pinned(self) -> None:
        root = Path(__file__).resolve().parents[2]
        versions = (root / "infra/kubernetes/kfp/versions.env").read_text()
        requirements = (
            root / "ai_platform/ml/pipelines/requirements-kfp.txt"
        ).read_text()
        dockerfile = (
            root / "infra/kubernetes/kfp/image/Dockerfile"
        ).read_text()

        self.assertIn("KFP_VERSION=2.17.3", versions)
        self.assertIn("KFP_SDK_VERSION=2.17.3", versions)
        self.assertEqual("kfp==2.17.3\nPyYAML==6.0.3\n", requirements)
        self.assertIn("FROM python:3.10.19-slim-bookworm", dockerfile)
        self.assertNotIn(":latest", versions + dockerfile)


if __name__ == "__main__":
    unittest.main()
