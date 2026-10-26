from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

import yaml

from ai_platform.ml.tracking.config import load_experiment_config
from ai_platform.ml.tracking.snapshot import load_snapshot_provenance


RUNTIME_ENV = {
    "F9_DATASET_PATH": "/tmp/canonical-training",
    "F9_OUTPUT_PATH": "/tmp/f9-evidence",
    "F9_TRAINING_SNAPSHOT_URI": (
        "gs://ewallet-data-ai-platform-hg26-data/ml/training-snapshots/"
        "canonical-100gb-v1/training-20261024-010203"
    ),
    "DATASET_VERSION": "canonical-100gb-v1",
    "SILVER_RUN_ID": "canonical-silver-20261024-000001",
    "FEATURE_RUN_ID": "canonical-features-20261024-003000",
    "SNAPSHOT_ID": "training-20261024-010203",
    "SNAPSHOT_MANIFEST_SHA256": "d" * 64,
    "MLFLOW_TRACKING_URI": "http://mlflow.ml-platform.svc.cluster.local:5000",
    "MLFLOW_EXPERIMENT_NAME": "ewallet-fraud-comparison",
    "MLFLOW_REGISTERED_MODEL_NAME": "ewallet-fraud-detector",
}


class CanonicalF9ConfigTest(unittest.TestCase):
    def test_checked_in_config_is_runtime_bound_and_exactly_four_candidates(self) -> None:
        config = load_experiment_config(
            Path("ai_platform/ml/config/f9_mlflow_experiment.yaml"),
            repo_root=Path.cwd(),
            environment=RUNTIME_ENV,
        )

        self.assertEqual(Path(RUNTIME_ENV["F9_DATASET_PATH"]), config.dataset_path)
        self.assertEqual(RUNTIME_ENV["SNAPSHOT_ID"], config.snapshot.snapshot_id)
        self.assertEqual(RUNTIME_ENV["FEATURE_RUN_ID"], config.snapshot.feature_run_id)
        self.assertEqual("winner_only", config.tracking.registry_mode)
        self.assertEqual(
            [
                "dummy_baseline",
                "logistic_regression",
                "random_forest",
                "hist_gradient_boosting",
            ],
            [candidate.model.name for candidate in config.candidates],
        )
        self.assertFalse(config.candidates[0].selection_eligible)
        self.assertTrue(all(c.selection_eligible for c in config.candidates[1:]))
        rendered = json.dumps(config.snapshot.as_dict())
        self.assertNotIn("localhost", rendered)
        self.assertNotIn("minio", rendered.lower())

    def test_unresolved_runtime_value_fails_closed(self) -> None:
        environment = dict(RUNTIME_ENV)
        environment.pop("SNAPSHOT_ID")
        with self.assertRaisesRegex(ValueError, "SNAPSHOT_ID"):
            load_experiment_config(
                Path("ai_platform/ml/config/f9_mlflow_experiment.yaml"),
                repo_root=Path.cwd(),
                environment=environment,
            )


class SnapshotProvenanceTest(unittest.TestCase):
    def test_manifest_lineage_counts_and_hashes_are_captured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest = {
                "schema_version": "f7-training-dataset-v1",
                "materialized_rows": 40_148,
                "split_summary": {
                    "train": {"rows": 28_000, "normal": 27_430, "fraud": 570},
                    "validation": {"rows": 6_000, "normal": 5_880, "fraud": 120},
                    "test": {"rows": 6_148, "normal": 6_018, "fraud": 130},
                },
                "schema": {"model_features": 41},
                "lineage": {
                    "dataset_version": RUNTIME_ENV["DATASET_VERSION"],
                    "source_silver_run_id": RUNTIME_ENV["SILVER_RUN_ID"],
                    "source_feature_run_id": RUNTIME_ENV["FEATURE_RUN_ID"],
                    "snapshot_id": RUNTIME_ENV["SNAPSHOT_ID"],
                },
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True), encoding="utf-8"
            )
            manifest_digest = hashlib.sha256(
                (root / "manifest.json").read_bytes()
            ).hexdigest()
            config = load_experiment_config(
                Path("ai_platform/ml/config/f9_mlflow_experiment.yaml"),
                repo_root=Path.cwd(),
                environment={
                    **RUNTIME_ENV,
                    "F9_DATASET_PATH": str(root),
                    "SNAPSHOT_MANIFEST_SHA256": manifest_digest,
                },
            )

            provenance = load_snapshot_provenance(root, config.snapshot)

        self.assertEqual(40_148, provenance.row_count)
        self.assertEqual(820, provenance.positive_count)
        self.assertEqual(39_328, provenance.negative_count)
        self.assertEqual(41, provenance.feature_count)
        self.assertEqual(64, len(provenance.manifest_sha256))
        self.assertEqual(64, len(provenance.feature_schema_sha256))

    def test_manifest_lineage_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "materialized_rows": 1,
                        "split_summary": {
                            name: {
                                "rows": 1 if name == "train" else 0,
                                "normal": 1 if name == "train" else 0,
                                "fraud": 0,
                            }
                            for name in ("train", "validation", "test")
                        },
                        "schema": {"model_features": 41},
                        "lineage": {
                            "dataset_version": "wrong-version",
                            "source_silver_run_id": RUNTIME_ENV["SILVER_RUN_ID"],
                            "source_feature_run_id": RUNTIME_ENV["FEATURE_RUN_ID"],
                            "snapshot_id": RUNTIME_ENV["SNAPSHOT_ID"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            manifest_digest = hashlib.sha256(
                (root / "manifest.json").read_bytes()
            ).hexdigest()
            config = load_experiment_config(
                Path("ai_platform/ml/config/f9_mlflow_experiment.yaml"),
                repo_root=Path.cwd(),
                environment={
                    **RUNTIME_ENV,
                    "F9_DATASET_PATH": str(root),
                    "SNAPSHOT_MANIFEST_SHA256": manifest_digest,
                },
            )
            with self.assertRaisesRegex(ValueError, "dataset_version"):
                load_snapshot_provenance(root, config.snapshot)

    def test_manifest_digest_mismatch_fails_before_training(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "manifest.json").write_text("{}\n", encoding="utf-8")
            config = load_experiment_config(
                Path("ai_platform/ml/config/f9_mlflow_experiment.yaml"),
                repo_root=Path.cwd(),
                environment={**RUNTIME_ENV, "F9_DATASET_PATH": str(root)},
            )
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                load_snapshot_provenance(root, config.snapshot)


class KubernetesMlflowContractTest(unittest.TestCase):
    def test_manifests_are_internal_bounded_and_reference_runtime_secrets(self) -> None:
        root = Path("infra/kubernetes/mlflow")
        documents = []
        for path in sorted(root.glob("*.yaml")):
            documents.extend(doc for doc in yaml.safe_load_all(path.read_text()) if doc)
        by_kind = {document["kind"]: document for document in documents}
        service = by_kind["Service"]
        deployment = by_kind["Deployment"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]

        self.assertEqual("ClusterIP", service["spec"]["type"])
        self.assertNotIn("Ingress", by_kind)
        self.assertNotIn("LoadBalancer", json.dumps(documents))
        self.assertEqual(1, deployment["spec"]["replicas"])
        self.assertIn("v3.17.1", container["image"])
        self.assertIn("readinessProbe", container)
        self.assertIn("livenessProbe", container)
        self.assertIn("requests", container["resources"])
        self.assertIn("limits", container["resources"])
        rendered = json.dumps(documents).lower()
        self.assertIn("mlflow-backend-store", rendered)
        self.assertIn("mlflow-runtime", rendered)
        self.assertNotIn("minio", rendered)
        self.assertNotIn("localhost", rendered)


if __name__ == "__main__":
    unittest.main()
