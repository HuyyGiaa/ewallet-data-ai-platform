from __future__ import annotations

import tempfile
import unittest
import zipfile
import json
from pathlib import Path
from unittest.mock import patch

from ai_platform.features.feast.cloud_boundary import CloudFeastTrainingBoundary
from ai_platform.features.feast.provenance import build_feature_selection_evidence
from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.config import RuntimeConfig, ServiceConfig, SparkConfig, StorageConfig
from data_platform.config.spark_profiles import FEATURES_STAGE, SILVER_STAGE
from data_platform.processing.spark.common.runtime_contract import (
    validate_gcp_spark_runtime,
)
from ops.gcp.package_python_job import build_python_archive
from ops.gcp.prepare_feast_training_boundary import (
    NEW_SNAPSHOT_AUTHORIZATION,
    NEW_SNAPSHOT_WORKFLOW,
    TRAINING_PHASE,
    TRAINING_SNAPSHOT_AUTHORIZATION,
    render_user_commands,
)
from ops.gcp.publish_training_snapshot import TrustedRegistryIdentity
from ops.gcp.submit_feast_training_export import (
    TRAINING_SOURCE_JOB,
    VALIDATION_SAMPLES_JOB,
    build_submission_command,
    require_job_pit_evidence,
)
from ops.gcp.submit_source_to_bronze import load_cloud_runtime
from tests.unit.test_submit_gold_cloud import OUTPUTS


class FeastTrainingGcpRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        selection = build_feature_selection_evidence(
            dataset_version="canonical-100gb-v1",
            silver_run_id="ab-20261025134342-optimized-silver-r261025181923",
            feature_run_id="ab-20261025134342-optimized-feat-r261026025955",
            feature_root=(
                "gs://example-project-data/lakehouse/runs/"
                "ab-20261025134342-optimized-feat-r261026025955/gold"
            ),
            spark_profile="optimized",
            git_sha="bbae48caef7f65fda0c9375a7a49afb2eca5eef4",
            selection_basis="canonical-dp3-pass",
            source_evidence_digest="sha256:" + "b" * 64,
        )
        self.boundary = CloudFeastTrainingBoundary(
            bucket="example-project-data",
            dataset_version=selection["dataset_version"],
            silver_run_id=selection["source_silver_run_id"],
            feature_run_id=selection["source_feature_run_id"],
            snapshot_id="canonical-training-v2-20261031-ml1",
            source_git_sha=selection["source_git_sha"],
            spark_profile=selection["spark_profile"],
            feature_selection_digest=selection["evidence_digest"],
            feast_snapshot_id="canonical-training-v1-20261028-101106-recovered",
        )
        self.config = RuntimeConfig(
            environment="gcp",
            project_id="example-project",
            region="asia-southeast1",
            storage=StorageConfig(
                scheme="gs",
                source_root="gs://example-project-data/source",
                bronze_root="gs://example-project-data/lakehouse/bronze",
                silver_root=self.boundary.silver_root,
                gold_root=self.boundary.feature_gold_root,
                feast_root="gs://example-project-data/features/offline",
                options={},
            ),
            spark=SparkConfig(
                master=None,
                shuffle_partitions=400,
                event_log_uri="gs://example-project-data/spark-events",
                packages=(),
                configurations={},
            ),
            services=ServiceConfig(
                datahub_gms_url="https://datahub.example.test",
                trino_host=None,
                trino_port=None,
            ),
        )
        self.registry_generation = 1793255806863364
        self.registry_sha256 = "d" * 64
        self.registry_execution_git_sha = "8" * 40

    def test_boundary_exposes_immutable_gcs_staging_roots(self) -> None:
        prefix = (
            "gs://example-project-data/ml/training-staging/canonical-100gb-v1/"
            "canonical-training-v2-20261031-ml1"
        )
        self.assertEqual(prefix, self.boundary.training_staging_root)
        self.assertEqual(
            f"{prefix}/validation", self.boundary.validation_staging_root
        )
        self.assertEqual(
            f"{prefix}/source", self.boundary.training_source_staging_root
        )

    def test_validation_export_uses_explicit_dp3_stage(self) -> None:
        from ai_platform.features.feast.sources import export_validation_samples

        with patch.object(
            export_validation_samples,
            "create_spark_session",
            side_effect=RuntimeError("stop-after-session-contract"),
        ) as create:
            with self.assertRaisesRegex(RuntimeError, "session-contract"):
                export_validation_samples.export_samples(
                    self.boundary.validation_staging_root,
                    100,
                    self.config,
                )
        self.assertEqual(FEATURES_STAGE, create.call_args.kwargs["stage"])

    def test_training_source_export_uses_explicit_dp2_silver_stage(self) -> None:
        from ai_platform.ml.training import export_training_source

        with patch(
            "data_platform.processing.spark.common.spark_session."
            "create_spark_session",
            side_effect=RuntimeError("stop-after-session-contract"),
        ) as create:
            with self.assertRaisesRegex(RuntimeError, "session-contract"):
                export_training_source.export_training_source(
                    self.boundary.training_source_staging_root,
                    10_000,
                    self.config,
                    dataset_version=self.boundary.dataset_version,
                    source_silver_run_id=self.boundary.silver_run_id,
                    snapshot_id=self.boundary.snapshot_id,
                )
        self.assertEqual(SILVER_STAGE, create.call_args.kwargs["stage"])

    def test_dataproc_commands_pin_runtime_delta_gcs_and_stage_profile(self) -> None:
        runtime = load_cloud_runtime(OUTPUTS)
        for job, batch, stage, output in (
            (
                VALIDATION_SAMPLES_JOB,
                "feast-validation-261028101106",
                FEATURES_STAGE,
                self.boundary.validation_staging_root,
            ),
            (
                TRAINING_SOURCE_JOB,
                "training-source-261028101106",
                SILVER_STAGE,
                self.boundary.training_source_staging_root,
            ),
        ):
            command = build_submission_command(
                runtime,
                job=job,
                batch_id=batch,
                boundary=self.boundary,
                script_path=Path(f"{job}.py"),
                archive_path=Path("job.zip"),
                runtime_profile_path=Path("platform.yaml"),
                jar_paths=(Path("delta.jar"), Path("storage.jar")),
            )
            self.assertIn("--version=2.2", command)
            self.assertIn(f"--project-id={runtime.project_id}", command)
            self.assertIn(f"--region={runtime.region}", command)
            self.assertIn(f"--data-bucket={runtime.data_bucket}", command)
            self.assertIn("--jars=delta.jar,storage.jar", command)
            self.assertIn("--py-files=job.zip", command)
            self.assertIn("--runtime-config-file=platform.yaml", command)
            self.assertIn(f"--output-root={output}", command)
            self.assertIn(
                f"--source-git-sha={self.boundary.source_git_sha}", command
            )
            self.assertIn("--execution-git-sha=" + "0" * 40, command)
            self.assertIn(
                "--feature-selection-digest="
                + self.boundary.feature_selection_digest,
                command,
            )
            properties = next(
                item.removeprefix("--properties=")
                for item in command
                if item.startswith("--properties=")
            )
            self.assertIn("dataproc.tier=standard", properties)
            self.assertIn("spark.sql.extensions=io.delta.sql", properties)
            self.assertIn("spark.eventLog.dir=gs://example-project-data/spark-events", properties)
            self.assertIn("spark.dynamicAllocation.maxExecutors=7", properties)
            self.assertIn("spark.sql.shuffle.partitions=", properties)
            self.assertIn(stage, " ".join(command))

    def test_python_archive_can_include_ai_platform_without_changing_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for package in ("data_platform", "ops", "ai_platform"):
                (root / package).mkdir()
                (root / package / "marker.py").write_text(
                    f"PACKAGE = {package!r}\n", encoding="utf-8"
                )
            destination = root / "job.zip"
            build_python_archive(
                root,
                destination,
                included_roots=("data_platform", "ops", "ai_platform"),
            )
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(
                    {
                        "ai_platform/marker.py",
                        "data_platform/marker.py",
                        "ops/marker.py",
                    },
                    set(archive.namelist()),
                )

    def _write_pit_evidence(self, path: Path, *, status: str = "PASS") -> None:
        path.write_text(
            json.dumps(
                {
                    "schema_version": "feast-pit-validation-evidence-v1",
                    "status": status,
                    "environment": "gcp",
                    "sample_rows": 10,
                    "distinct_sample_cases": 10,
                    "historical_features_per_case": 31,
                    "expected_comparable_values": 310,
                    "comparable_values": 310,
                    "mismatch_count": 0,
                    "labels_passed_to_feast": False,
                    "validation_export_lineage": {
                        "dataset_version": self.boundary.dataset_version,
                        "source_silver_run_id": self.boundary.silver_run_id,
                        "source_feature_run_id": self.boundary.feature_run_id,
                        "source_silver_root": self.boundary.silver_root,
                        "source_feature_root": self.boundary.feature_gold_root,
                        "snapshot_id": self.boundary.resolved_feast_snapshot_id,
                        "source_git_sha": self.boundary.source_git_sha,
                        "execution_git_sha": "8" * 40,
                        "feature_selection_digest": (
                            self.boundary.feature_selection_digest
                        ),
                    },
                    "registry_readback": {
                        "status": "READBACK_VERIFIED",
                        "registry_uri": self.boundary.feast_registry_uri,
                        "registry_generation": self.registry_generation,
                        "registry_sha256": self.registry_sha256,
                    },
                }
            ),
            encoding="utf-8",
        )

    def _write_registry_evidence(self, path: Path) -> TrustedRegistryIdentity:
        path.write_text(
            json.dumps(
                {
                    "schema_version": "feast-registry-operation-evidence-v1",
                    "status": "READBACK_VERIFIED",
                    "mode": "readback-only",
                    "registry_uri": self.boundary.feast_registry_uri,
                    "snapshot_id": self.boundary.resolved_feast_snapshot_id,
                    "snapshot_root": self.boundary.feast_snapshot_root,
                    "execution_git_sha": self.registry_execution_git_sha,
                    "registry_generation": self.registry_generation,
                    "registry_sha256": self.registry_sha256,
                    "historical_feature_count": 31,
                    "model_input_count": 41,
                    "feature_views": [{"name": name} for name in FEATURE_SPECS],
                }
            ),
            encoding="utf-8",
        )
        return TrustedRegistryIdentity(
            readback_evidence=path,
            uri=self.boundary.feast_registry_uri,
            generation=self.registry_generation,
            sha256=self.registry_sha256,
            execution_git_sha=self.registry_execution_git_sha,
        )

    def test_default_sequence_is_pit_only(self) -> None:
        commands = render_user_commands(
            self.boundary,
            "runtime/cloud-training",
            feature_selection_evidence="evidence/feature-selection.json",
        )
        rendered = "\n".join(commands)
        self.assertEqual(1, rendered.count("ops.gcp.submit_feast_training_export"))
        self.assertIn("--job validation-samples", rendered)
        self.assertNotIn("--job training-source", rendered)
        self.assertIn(self.boundary.validation_staging_root, rendered)
        self.assertIn("gcloud storage rsync --recursive", rendered)
        self.assertNotIn("ai_platform.ml.training.materialize", rendered)
        self.assertNotIn("ops.gcp.publish_training_snapshot", rendered)
        self.assertNotIn(self.boundary.training_snapshot_root, rendered)
        self.assertIn("--registry-readback-evidence", rendered)
        self.assertIn("--expected-registry-generation", rendered)
        self.assertIn("--expected-registry-sha256", rendered)
        self.assertIn("--expected-execution-git-sha", rendered)
        self.assertIn("--evidence-output", rendered)
        self.assertIn("ops.gcp.publish_feast_registry", rendered)
        self.assertEqual(1, rendered.count(" readback-only"))
        self.assertEqual(1, rendered.count('= SUCCEEDED'))
        self.assertNotIn(
            "ai_platform.features.feast.sources.export_validation_samples",
            rendered,
        )
        self.assertNotIn("ai_platform.ml.training.export_training_source", rendered)

    def test_training_phase_requires_pit_pass_and_publication_authorization(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit authorization"):
            render_user_commands(
                self.boundary,
                "runtime/cloud-training",
                feature_selection_evidence="evidence/feature-selection.json",
                phase=TRAINING_PHASE,
            )
        with self.assertRaisesRegex(ValueError, "PIT PASS evidence"):
            render_user_commands(
                self.boundary,
                "runtime/cloud-training",
                feature_selection_evidence="evidence/feature-selection.json",
                phase=TRAINING_PHASE,
                training_snapshot_authorization=TRAINING_SNAPSHOT_AUTHORIZATION,
            )
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "pit.json"
            self._write_pit_evidence(evidence, status="FAIL")
            identity = self._write_registry_evidence(
                Path(temporary) / "registry.json"
            )
            with self.assertRaisesRegex(ValueError, "canonical PASS"):
                render_user_commands(
                    self.boundary,
                    "runtime/cloud-training",
                    feature_selection_evidence="evidence/feature-selection.json",
                    phase=TRAINING_PHASE,
                    pit_evidence=evidence,
                    registry_identity=identity,
                    training_snapshot_authorization=(
                        TRAINING_SNAPSHOT_AUTHORIZATION
                    ),
                )

    def test_authorized_training_phase_uses_create_only_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "pit.json"
            self._write_pit_evidence(evidence)
            identity = self._write_registry_evidence(
                Path(temporary) / "registry.json"
            )
            commands = render_user_commands(
                self.boundary,
                "runtime/cloud-training",
                feature_selection_evidence="evidence/feature-selection.json",
                phase=TRAINING_PHASE,
                pit_evidence=evidence,
                registry_identity=identity,
                training_snapshot_authorization=TRAINING_SNAPSHOT_AUTHORIZATION,
            )
        rendered = "\n".join(commands)
        self.assertIn("--job training-source", rendered)
        self.assertIn("ai_platform.ml.training.materialize", rendered)
        self.assertIn("ops.gcp.publish_training_snapshot", rendered)
        self.assertIn("CREATE_ONLY_TRAINING_SNAPSHOT", rendered)
        self.assertIn("--pit-evidence", rendered)
        self.assertEqual(2, rendered.count("--pit-evidence"))
        self.assertNotIn("--job validation-samples", rendered)
        self.assertNotIn("gcloud storage ls", rendered)
        self.assertNotIn(
            f"gcloud storage rsync --recursive", rendered.splitlines()[-1]
        )

    def test_training_source_submitter_independently_requires_pit_pass(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires PIT PASS evidence"):
            require_job_pit_evidence(TRAINING_SOURCE_JOB, None, self.boundary)
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "pit.json"
            self._write_pit_evidence(evidence)
            identity = self._write_registry_evidence(
                Path(temporary) / "registry.json"
            )
            require_job_pit_evidence(
                TRAINING_SOURCE_JOB,
                evidence,
                self.boundary,
                identity,
            )
            with self.assertRaisesRegex(ValueError, "only for training-source"):
                require_job_pit_evidence(
                    VALIDATION_SAMPLES_JOB,
                    evidence,
                    self.boundary,
                    identity,
                )

    def test_recovered_snapshot_sequence_cannot_export_apply_or_publish_registry(
        self,
    ) -> None:
        commands = render_user_commands(
            self.boundary,
            "runtime/cloud-training",
            feature_selection_evidence="evidence/feature-selection.json",
        )
        rendered = "\n".join(commands)
        self.assertNotIn("export_gold_to_parquet", rendered)
        self.assertNotIn(" feast ", f" {rendered} ")
        self.assertNotIn(" plan", rendered)
        self.assertNotIn(" apply", rendered)
        self.assertNotIn(" publish --authorization ", rendered)
        self.assertNotIn("CREATE_ONLY_FEAST_REGISTRY", rendered)
        self.assertIn(" readback-only", rendered)
        self.assertIn("--trusted-registry-artifact", rendered)

    def test_new_snapshot_sequence_requires_separate_explicit_authorization(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "explicit authorization"):
            render_user_commands(
                self.boundary,
                "runtime/cloud-training",
                feature_selection_evidence="evidence/feature-selection.json",
                workflow=NEW_SNAPSHOT_WORKFLOW,
            )

        commands = render_user_commands(
            self.boundary,
            "runtime/cloud-training",
            feature_selection_evidence="evidence/feature-selection.json",
            workflow=NEW_SNAPSHOT_WORKFLOW,
            new_snapshot_authorization=NEW_SNAPSHOT_AUTHORIZATION,
        )
        rendered = "\n".join(commands)
        self.assertIn("export_gold_to_parquet", rendered)
        self.assertIn(" plan", rendered)
        self.assertIn(" publish --authorization CREATE_ONLY_FEAST_REGISTRY", rendered)
        self.assertIn(" readback-only", rendered)
        self.assertNotIn(" feast ", f" {rendered} ")

    def test_gcp_runtime_rejects_local_spark_master(self) -> None:
        class Context:
            master = "local[*]"

        class Spark:
            sparkContext = Context()

        with self.assertRaisesRegex(RuntimeError, "local Spark master"):
            validate_gcp_spark_runtime(
                Spark(),
                self.config,
                output_uri=self.boundary.training_source_staging_root,
            )

    def test_gcp_runtime_rejects_missing_delta_configuration(self) -> None:
        class Context:
            master = "k8s://managed-spark"

        class Conf:
            def get(self, key: str) -> str:
                return "missing"

        class Spark:
            sparkContext = Context()
            conf = Conf()

        with self.assertRaisesRegex(RuntimeError, "spark.sql.extensions"):
            validate_gcp_spark_runtime(
                Spark(),
                self.config,
                output_uri=self.boundary.training_source_staging_root,
            )

    def test_gcp_runtime_rejects_missing_gcs_connector(self) -> None:
        class Context:
            master = "k8s://managed-spark"

        class Conf:
            values = {
                "spark.sql.extensions": "io.delta.sql.DeltaSparkSessionExtension",
                "spark.sql.catalog.spark_catalog": (
                    "org.apache.spark.sql.delta.catalog.DeltaCatalog"
                ),
            }

            def get(self, key: str) -> str:
                return self.values[key]

        class Scheme:
            def getScheme(self) -> str:
                return "file"

        class Filesystem:
            def getUri(self) -> Scheme:
                return Scheme()

        class HadoopPath:
            def getFileSystem(self, _configuration: object) -> Filesystem:
                return Filesystem()

        class PathFactory:
            def __call__(self, _uri: str) -> HadoopPath:
                return HadoopPath()

        class HadoopFs:
            Path = PathFactory()

        class Hadoop:
            fs = HadoopFs()

        class Apache:
            hadoop = Hadoop()

        class Org:
            apache = Apache()

        class Jvm:
            org = Org()

        class Jsc:
            def hadoopConfiguration(self) -> object:
                return object()

        class Spark:
            sparkContext = Context()
            conf = Conf()
            _jvm = Jvm()
            _jsc = Jsc()

        with self.assertRaisesRegex(RuntimeError, "Cloud Storage connector"):
            validate_gcp_spark_runtime(
                Spark(),
                self.config,
                output_uri=self.boundary.training_source_staging_root,
            )

    def test_incomplete_validation_export_manifest_is_rejected(self) -> None:
        from ai_platform.features.feast.validation.contracts import (
            require_validation_export_manifest,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "feast-validation-samples-v1",
                        "status": "PARTIAL",
                        "canonical_sample_rows": 10,
                        "benchmark_rows": 100,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "incomplete"):
                require_validation_export_manifest(root)

    def test_training_source_manifest_must_match_canonical_lineage(self) -> None:
        from ai_platform.ml.training.source_contract import (
            validate_source_manifest_contract,
        )

        manifest = {
            "schema_version": "f7-training-source-v1",
            "status": "COMPLETE",
            "scope": "hash_bounded",
            "sample_modulus": 10_000,
            "written_rows": 36_960,
            "output_columns": ["transaction_id"],
            "selected_split_profile": {
                "train": {"rows": 1},
                "validation": {"rows": 1},
                "test": {"rows": 1},
            },
            "lineage": {
                "dataset_version": self.boundary.dataset_version,
                "source_silver_run_id": self.boundary.silver_run_id,
                "source_feature_run_id": self.boundary.feature_run_id,
                "snapshot_id": self.boundary.snapshot_id,
                "source_git_sha": self.boundary.source_git_sha,
                "feature_selection_digest": self.boundary.feature_selection_digest,
            },
        }
        validate_source_manifest_contract(
            manifest,
            dataset_version=self.boundary.dataset_version,
            source_silver_run_id=self.boundary.silver_run_id,
            source_feature_run_id=self.boundary.feature_run_id,
            snapshot_id=self.boundary.snapshot_id,
            source_git_sha=self.boundary.source_git_sha,
            feature_selection_digest=self.boundary.feature_selection_digest,
            expected_output_columns=("transaction_id",),
        )
        manifest["lineage"]["snapshot_id"] = "stale-snapshot"
        with self.assertRaisesRegex(ValueError, "lineage"):
            validate_source_manifest_contract(
                manifest,
                dataset_version=self.boundary.dataset_version,
                source_silver_run_id=self.boundary.silver_run_id,
                source_feature_run_id=self.boundary.feature_run_id,
                snapshot_id=self.boundary.snapshot_id,
                source_git_sha=self.boundary.source_git_sha,
                feature_selection_digest=self.boundary.feature_selection_digest,
                expected_output_columns=("transaction_id",),
            )


if __name__ == "__main__":
    unittest.main()
