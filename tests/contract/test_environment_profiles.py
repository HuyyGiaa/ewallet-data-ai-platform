from __future__ import annotations

import re
import unittest
from collections.abc import Iterator, Mapping
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import yaml

from data_platform.config import load_runtime_config
from data_platform.config.cli import main as config_cli_main


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPOSITORY_ROOT / "configs" / "environments"
PROFILE_NAMES = ("local", "dev", "gcp")

LOCAL_ENVIRONMENT = {
    "MINIO_ACCESS_KEY": "contract-access-key",
    "MINIO_SECRET_KEY": "contract-secret-key",
}
GCP_ENVIRONMENT = {
    "GCP_PROJECT_ID": "contract-project",
    "GCP_REGION": "asia-southeast1",
    "EWALLET_DATA_BUCKET": "contract-ewallet-data",
    "DATAHUB_GMS_URL": "http://datahub-gms.metadata.svc.cluster.local:8080",
}

_PLACEHOLDER_ONLY = re.compile(
    r"^\$\{[A-Z_][A-Z0-9_]*(?::-[^}]*)?\}$"
)
_SENSITIVE_KEY = re.compile(
    r"(?:password|secret|token|access[._-]?key|private[._-]?key)",
    re.IGNORECASE,
)


def _walk_scalars(
    value: object,
    path: tuple[str, ...] = (),
) -> Iterator[tuple[tuple[str, ...], object]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk_scalars(item, (*path, str(key)))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_scalars(item, (*path, str(index)))
        return
    yield path, value


class EnvironmentProfileContractTest(unittest.TestCase):
    def profile_path(self, environment: str) -> Path:
        return CONFIG_ROOT / environment / "platform.yaml"

    def test_all_environment_profile_files_exist(self) -> None:
        for environment in PROFILE_NAMES:
            with self.subTest(environment=environment):
                self.assertTrue(
                    self.profile_path(environment).is_file(),
                    f"missing environment profile: {environment}/platform.yaml",
                )

    def test_local_profile_uses_expected_minio_roots(self) -> None:
        config = load_runtime_config(
            "local",
            config_root=CONFIG_ROOT,
            environ=LOCAL_ENVIRONMENT,
        )

        self.assertEqual(config.storage.scheme, "s3a")
        self.assertEqual(config.storage.bronze_root, "s3a://bronze-zone")
        self.assertEqual(config.storage.silver_root, "s3a://silver-zone")
        self.assertEqual(config.storage.gold_root, "s3a://gold-zone")
        self.assertEqual(config.spark.master, "local[4]")

    def test_local_profile_requires_both_minio_credentials(self) -> None:
        for missing_variable in ("MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"):
            environment = dict(LOCAL_ENVIRONMENT)
            environment.pop(missing_variable)

            with self.subTest(missing_variable=missing_variable):
                with self.assertRaisesRegex(ValueError, missing_variable):
                    load_runtime_config(
                        "local",
                        config_root=CONFIG_ROOT,
                        environ=environment,
                    )

    def test_dev_and_gcp_profiles_use_bucket_scoped_gcs_roots(self) -> None:
        expected_prefix = "gs://contract-ewallet-data/"

        for environment_name in ("dev", "gcp"):
            with self.subTest(environment=environment_name):
                config = load_runtime_config(
                    environment_name,
                    config_root=CONFIG_ROOT,
                    environ=GCP_ENVIRONMENT,
                )

                self.assertEqual(config.storage.scheme, "gs")
                for root in (
                    config.storage.source_root,
                    config.storage.bronze_root,
                    config.storage.silver_root,
                    config.storage.gold_root,
                    config.storage.feast_root,
                    config.spark.event_log_uri,
                ):
                    self.assertTrue(root.startswith(expected_prefix), root)
                self.assertIsNone(config.spark.master)

    def test_gcp_profile_requires_cloud_identity_and_service_values(self) -> None:
        for missing_variable in (
            "GCP_PROJECT_ID",
            "EWALLET_DATA_BUCKET",
            "DATAHUB_GMS_URL",
        ):
            environment = dict(GCP_ENVIRONMENT)
            environment.pop(missing_variable)

            with self.subTest(missing_variable=missing_variable):
                with self.assertRaisesRegex(ValueError, missing_variable):
                    load_runtime_config(
                        "gcp",
                        config_root=CONFIG_ROOT,
                        environ=environment,
                    )

    def test_profiles_do_not_commit_secrets_or_developer_paths(self) -> None:
        for environment in PROFILE_NAMES:
            path = self.profile_path(environment)
            with self.subTest(environment=environment):
                raw_text = path.read_text(encoding="utf-8")
                self.assertNotIn("/home/", raw_text)
                self.assertNotIn("huyy-giaa", raw_text)

                parsed = yaml.safe_load(raw_text)
                for key_path, value in _walk_scalars(parsed):
                    dotted_key = ".".join(key_path)
                    if _SENSITIVE_KEY.search(dotted_key):
                        self.assertIsInstance(value, str, dotted_key)
                        self.assertRegex(value, _PLACEHOLDER_ONLY, dotted_key)

    def test_preflight_cli_reports_redacted_gcp_profile(self) -> None:
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = config_cli_main(
                ["--environment", "gcp", "--check"],
                environ=GCP_ENVIRONMENT,
            )

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0, stderr.getvalue())
        self.assertIn("environment: gcp", output)
        self.assertIn("scheme: gs", output)
        self.assertIn("gold_root: gs://<redacted>/lakehouse/gold", output)
        self.assertIn("CONFIG CHECK PASS", output)
        self.assertNotIn("contract-ewallet-data", output)
        self.assertEqual(stderr.getvalue(), "")

    def test_preflight_cli_fails_without_required_bucket(self) -> None:
        environment = dict(GCP_ENVIRONMENT)
        environment.pop("EWALLET_DATA_BUCKET")
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = config_cli_main(
                ["--environment", "gcp", "--check"],
                environ=environment,
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("EWALLET_DATA_BUCKET", stderr.getvalue())
        self.assertNotIn("contract-project", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
