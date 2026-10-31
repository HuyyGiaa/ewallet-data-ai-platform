"""Persist one model comparison as parent/child MLflow runs."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import mlflow
import pandas as pd
from mlflow.models import infer_signature

from ai_platform.ml.modeling.experiment import ExperimentResult
from ai_platform.ml.tracking.model import FraudPyFuncModel


REQUIRED_PROVENANCE = {
    "dataset_version",
    "silver_run_id",
    "feature_run_id",
    "snapshot_id",
    "training_snapshot_uri",
    "snapshot_manifest_sha256",
    "snapshot_row_count",
    "feature_count",
    "feature_schema_sha256",
    "git_commit_sha",
    "random_seed",
    "dvc_revision",
    "runtime_image",
}
# Registered automatically for downstream staging deploys. Production is a
# separate, human-approved alias and is never set by the pipeline.
CANDIDATE_ALIAS = "candidate"


@dataclass(frozen=True)
class TrackingResult:
    experiment_id: str
    parent_run_id: str
    candidate_run_ids: Mapping[str, str]
    winner_model_uri: str
    registered_model_version: str | None


def python_package_snapshot(target: Path, package: Path = Path("ai_platform")) -> Path:
    """Copy only the package's Python modules for MLflow ``code_paths``.

    Copying the directory itself would also bundle nested virtualenvs, data
    and caches into every logged model.
    """

    destination = target / package.name
    for source in sorted(package.rglob("*.py")):
        relative = source.relative_to(package)
        if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
    return destination


class MlflowTracker:
    """Own MLflow persistence without participating in model selection."""

    def __init__(
        self,
        *,
        tracking_uri: str,
        experiment_name: str,
        registered_model_name: str,
        registry_mode: str,
    ) -> None:
        if registry_mode not in {"manual", "winner_only"}:
            raise ValueError("registry_mode must be manual or winner_only")
        self.tracking_uri = tracking_uri
        self.experiment_name = experiment_name
        self.registered_model_name = registered_model_name
        self.registry_mode = registry_mode

    def publish(
        self,
        result: ExperimentResult,
        *,
        input_example: pd.DataFrame,
        provenance: Mapping[str, object],
    ) -> TrackingResult:
        missing = sorted(REQUIRED_PROVENANCE.difference(provenance))
        if missing:
            raise ValueError(f"Missing required MLflow provenance: {missing}")
        mlflow.set_tracking_uri(self.tracking_uri)
        experiment = mlflow.set_experiment(self.experiment_name)
        candidate_run_ids: dict[str, str] = {}
        run_name = "fraud-comparison-" + datetime.now(timezone.utc).strftime(
            "%Y%m%d-%H%M%S"
        )
        with mlflow.start_run(
            experiment_id=experiment.experiment_id, run_name=run_name
        ) as parent:
            mlflow.set_tags(
                {
                    "project": "ewallet-data-ai-platform",
                    "workflow": "f9-fraud-comparison",
                    "winner": result.winner.name,
                    "registry_mode": self.registry_mode,
                }
            )
            mlflow.log_params({key: str(value) for key, value in provenance.items()})
            mlflow.log_dict(result.test_report, "reports/winner_test.json")
            for candidate in result.candidates:
                with mlflow.start_run(
                    experiment_id=experiment.experiment_id,
                    run_name=candidate.name,
                    nested=True,
                ) as child:
                    candidate_run_ids[candidate.name] = child.info.run_id
                    report = candidate.validation_report
                    metrics = report["metrics"]
                    mlflow.set_tags(
                        {
                            "candidate": candidate.name,
                            "estimator": candidate.estimator,
                            "selection_eligible": str(
                                candidate.selection_eligible
                            ).lower(),
                            "winner": str(candidate.tested).lower(),
                        }
                    )
                    mlflow.log_params(
                        {
                            "estimator": candidate.estimator,
                            "model_name": candidate.name,
                            **{
                                key: str(value)
                                for key, value in provenance.items()
                            },
                            **{
                                f"model.{key}": value
                                for key, value in report["parameters"].items()
                            },
                        }
                    )
                    mlflow.log_metrics(
                        {
                            "validation_pr_auc": float(metrics["pr_auc"]),
                            "validation_roc_auc": float(metrics["roc_auc"]),
                            "validation_f1": float(metrics["f1"]),
                            "validation_precision": float(metrics["precision"]),
                            "validation_recall": float(metrics["recall"]),
                            "validation_threshold": float(candidate.threshold),
                            "fit_seconds": float(report["fit_seconds"]),
                            "validation_predict_seconds": float(
                                report["predict_seconds"]
                            ),
                        }
                    )
                    mlflow.log_dict(report, "reports/validation.json")
                    if candidate.tested:
                        test_metrics = result.test_report["metrics"]
                        mlflow.log_metrics(
                            {
                                "test_pr_auc": float(test_metrics["pr_auc"]),
                                "test_roc_auc": float(test_metrics["roc_auc"]),
                                "test_f1": float(test_metrics["f1"]),
                                "test_precision": float(test_metrics["precision"]),
                                "test_recall": float(test_metrics["recall"]),
                                "test_threshold": float(candidate.threshold),
                                "test_predict_seconds": float(
                                    result.test_report["predict_seconds"]
                                ),
                            }
                        )
                        mlflow.log_dict(
                            result.test_report, "reports/winner_test.json"
                        )

            output_example = FraudPyFuncModel(
                result.winner.pipeline, result.winner.threshold
            ).predict(None, input_example)
            signature = infer_signature(input_example, output_example)
            code_root = Path(tempfile.mkdtemp(prefix="f9-code-"))
            try:
                model_info = mlflow.pyfunc.log_model(
                    name="winner_model",
                    python_model=FraudPyFuncModel(
                        result.winner.pipeline, result.winner.threshold
                    ),
                    input_example=input_example,
                    signature=signature,
                    code_paths=[str(python_package_snapshot(code_root))],
                    pip_requirements=[
                        "mlflow==3.17.1",
                        "numpy==2.2.6",
                        "pandas==2.3.3",
                        "scikit-learn==1.7.2",
                        "cloudpickle==3.1.2",
                    ],
                    metadata={
                        "threshold": result.winner.threshold,
                        "winner": result.winner.name,
                    },
                )
            finally:
                shutil.rmtree(code_root, ignore_errors=True)
            version: str | None = None
            if self.registry_mode == "winner_only":
                registered = mlflow.register_model(
                    model_info.model_uri, self.registered_model_name
                )
                version = str(registered.version)
                client = mlflow.MlflowClient()
                for key in (
                    "dataset_version",
                    "feature_run_id",
                    "snapshot_id",
                    "snapshot_manifest_sha256",
                    "git_commit_sha",
                    "dvc_revision",
                    "runtime_image",
                ):
                    client.set_model_version_tag(
                        self.registered_model_name,
                        version,
                        key,
                        str(provenance[key]),
                    )
                client.set_registered_model_alias(
                    self.registered_model_name, CANDIDATE_ALIAS, version
                )
            return TrackingResult(
                experiment_id=experiment.experiment_id,
                parent_run_id=parent.info.run_id,
                candidate_run_ids=dict(candidate_run_ids),
                winner_model_uri=model_info.model_uri,
                registered_model_version=version,
            )
