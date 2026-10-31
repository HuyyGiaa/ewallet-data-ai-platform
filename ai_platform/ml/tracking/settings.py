"""Fail-closed deployment settings for the canonical MLflow server."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlparse


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _pinned_image(value: str, name: str) -> str:
    if value.endswith(":latest") or (":" not in value and "@sha256:" not in value):
        raise ValueError(f"{name} must use an explicit non-latest tag or digest")
    return value


@dataclass(frozen=True)
class MlflowDeploymentSettings:
    project_id: str
    region: str
    tracking_uri: str
    backend_store_uri: str = field(repr=False)
    artifact_root: str
    image: str
    experiment_name: str
    registered_model_name: str

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str]
    ) -> "MlflowDeploymentSettings":
        tracking_uri = _required(environment, "MLFLOW_TRACKING_URI")
        tracking = urlparse(tracking_uri)
        if tracking.scheme not in {"http", "https"} or tracking.hostname in {
            "localhost",
            "127.0.0.1",
        }:
            raise ValueError("MLFLOW_TRACKING_URI must be an internal cloud service URI")
        backend = _required(environment, "MLFLOW_BACKEND_STORE_URI")
        if not backend.startswith("postgresql+psycopg2://"):
            raise ValueError("MLFLOW_BACKEND_STORE_URI must use PostgreSQL")
        artifact_root = _required(environment, "MLFLOW_ARTIFACT_ROOT")
        if not artifact_root.startswith("gs://"):
            raise ValueError("MLFLOW_ARTIFACT_ROOT must be a GCS URI")
        return cls(
            project_id=_required(environment, "GCP_PROJECT_ID"),
            region=_required(environment, "GCP_REGION"),
            tracking_uri=tracking_uri,
            backend_store_uri=backend,
            artifact_root=artifact_root,
            image=_pinned_image(_required(environment, "MLFLOW_IMAGE"), "MLFLOW_IMAGE"),
            experiment_name=_required(environment, "MLFLOW_EXPERIMENT_NAME"),
            registered_model_name=_required(
                environment, "MLFLOW_REGISTERED_MODEL_NAME"
            ),
        )
