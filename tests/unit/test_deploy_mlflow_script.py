from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
F9_IMAGE = (
    "asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/"
    "ewallet-platform/f9-kfp-runtime:kfp-2.17.3-f9-v1"
)
MLFLOW_IMAGE = (
    "asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/"
    "ewallet-platform/mlflow:v3.17.1-gcs-postgres-1"
)


class DeployMlflowScriptTest(unittest.TestCase):
    def test_execute_applies_manifest_when_image_is_already_canonical(self) -> None:
        """A no-op local image update must not feed an empty manifest to apply."""
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            command_log = temporary_path / "kubectl.log"
            fake_kubectl = temporary_path / "kubectl"
            fake_kubectl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -Eeuo pipefail
                    printf '%s\\n' "$*" >>"${KUBECTL_LOG}"

                    if [[ "$*" == *"set image"* ]]; then
                      # Real kubectl emits nothing when the requested image is unchanged.
                      exit 0
                    fi

                    if [[ "$*" == *"create secret generic"* || "$*" == *"create configmap"* ]]; then
                      printf '%s\\n' 'apiVersion: v1' 'kind: ConfigMap' 'metadata:' '  name: fixture'
                      exit 0
                    fi

                    if [[ "$*" == *"apply -f -"* ]]; then
                      payload="$(cat)"
                      if [[ -z "${payload}" ]]; then
                        echo 'error: no objects passed to apply' >&2
                        exit 1
                      fi
                    fi
                    """
                )
            )
            fake_kubectl.chmod(0o755)

            password = "a-secure-runtime-password-123456"
            environment = {
                **os.environ,
                "PATH": f"{temporary_path}:{os.environ['PATH']}",
                "KUBECTL_LOG": str(command_log),
                "GCP_PROJECT_ID": "ewallet-data-ai-platform-hg26",
                "GCP_REGION": "asia-southeast1",
                "F9_RUNTIME_IMAGE": F9_IMAGE,
                "MLFLOW_IMAGE": MLFLOW_IMAGE,
                "MLFLOW_ARTIFACT_ROOT": (
                    "gs://ewallet-data-ai-platform-hg26-data/mlflow/artifacts"
                ),
                "MLFLOW_DB_USERNAME": "mlflow",
                "MLFLOW_DB_PASSWORD": password,
            }
            result = subprocess.run(
                [str(REPO_ROOT / "scripts/mlops/deploy_mlflow.sh"), "--execute"],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            commands = command_log.read_text()
            self.assertIn(
                f"apply -f {REPO_ROOT}/infra/kubernetes/mlflow/deployment.yaml",
                commands,
            )
            self.assertNotIn("set image", commands)
            self.assertNotIn(password, output)


if __name__ == "__main__":
    unittest.main()
