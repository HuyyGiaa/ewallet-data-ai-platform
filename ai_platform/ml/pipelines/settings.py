"""Fail-closed deployment settings for standalone canonical KFP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ai_platform.ml.tracking.settings import _pinned_image, _required


@dataclass(frozen=True)
class KfpDeploymentSettings:
    project_id: str
    region: str
    cluster: str
    namespace: str
    runner_ksa: str
    training_gsa: str
    runtime_image: str
    version: str

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str]
    ) -> "KfpDeploymentSettings":
        version = _required(environment, "KFP_VERSION")
        if version != "2.17.3":
            raise ValueError("KFP_VERSION must remain pinned to 2.17.3")
        return cls(
            project_id=_required(environment, "GCP_PROJECT_ID"),
            region=_required(environment, "GCP_REGION"),
            cluster=_required(environment, "GKE_CLUSTER"),
            namespace=_required(environment, "KFP_NAMESPACE"),
            runner_ksa=_required(environment, "KFP_RUNNER_KSA"),
            training_gsa=_required(environment, "TRAINING_GSA"),
            runtime_image=_pinned_image(
                _required(environment, "F9_RUNTIME_IMAGE"), "F9_RUNTIME_IMAGE"
            ),
            version=version,
        )
