"""Canonical standalone Kubeflow Pipelines DAG for bounded F9 training."""

from kfp import dsl
from kfp.dsl import Artifact, Input, Model, Output


KFP_VERSION = "2.17.3"
F9_RUNTIME_IMAGE = (
    "asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/"
    "ewallet-platform/f9-kfp-runtime:kfp-2.17.3-f9-v1"
)
CANDIDATE_NAMES = (
    "dummy_baseline",
    "logistic_regression",
    "random_forest",
    "hist_gradient_boosting",
)
RUNTIME_MODULE = "ai_platform.ml.pipelines.runtime"


def _common_arguments(
    dataset_version: str,
    silver_run_id: str,
    feature_run_id: str,
    snapshot_id: str,
    snapshot_uri: str,
    snapshot_manifest_sha256: str,
    mlflow_tracking_uri: str,
) -> list[str]:
    return [
        "--dataset-version", dataset_version,
        "--silver-run-id", silver_run_id,
        "--feature-run-id", feature_run_id,
        "--snapshot-id", snapshot_id,
        "--snapshot-uri", snapshot_uri,
        "--snapshot-manifest-sha256", snapshot_manifest_sha256,
        "--mlflow-tracking-uri", mlflow_tracking_uri,
    ]


def _runtime_spec(command: str, arguments: list[str]) -> dsl.ContainerSpec:
    return dsl.ContainerSpec(
        image=F9_RUNTIME_IMAGE,
        command=["python", "-m", RUNTIME_MODULE, command],
        args=arguments,
    )


@dsl.container_component
def validate_snapshot(
    dataset_version: str,
    silver_run_id: str,
    feature_run_id: str,
    snapshot_id: str,
    snapshot_uri: str,
    snapshot_manifest_sha256: str,
    mlflow_tracking_uri: str,
    validation: Output[Artifact],
) -> dsl.ContainerSpec:
    return _runtime_spec(
        "validate-snapshot",
        _common_arguments(
            dataset_version, silver_run_id, feature_run_id, snapshot_id,
            snapshot_uri, snapshot_manifest_sha256, mlflow_tracking_uri,
        ) + ["--output", validation.path],
    )


@dsl.container_component
def verify_chronological_splits(
    dataset_version: str,
    silver_run_id: str,
    feature_run_id: str,
    snapshot_id: str,
    snapshot_uri: str,
    snapshot_manifest_sha256: str,
    mlflow_tracking_uri: str,
    split_validation: Output[Artifact],
) -> dsl.ContainerSpec:
    return _runtime_spec(
        "verify-chronological-splits",
        _common_arguments(
            dataset_version, silver_run_id, feature_run_id, snapshot_id,
            snapshot_uri, snapshot_manifest_sha256, mlflow_tracking_uri,
        ) + ["--output", split_validation.path],
    )


def _train_spec(
    candidate: str,
    dataset_version: str,
    silver_run_id: str,
    feature_run_id: str,
    snapshot_id: str,
    snapshot_uri: str,
    snapshot_manifest_sha256: str,
    mlflow_tracking_uri: str,
    model: Output[Model],
) -> dsl.ContainerSpec:
    return _runtime_spec(
        "train-candidate",
        _common_arguments(
            dataset_version, silver_run_id, feature_run_id, snapshot_id,
            snapshot_uri, snapshot_manifest_sha256, mlflow_tracking_uri,
        ) + ["--candidate", candidate, "--output", model.path],
    )


@dsl.container_component
def train_dummy_baseline(
    dataset_version: str, silver_run_id: str, feature_run_id: str,
    snapshot_id: str, snapshot_uri: str, snapshot_manifest_sha256: str,
    mlflow_tracking_uri: str, model: Output[Model],
) -> dsl.ContainerSpec:
    return _train_spec(
        "dummy_baseline", dataset_version, silver_run_id, feature_run_id,
        snapshot_id, snapshot_uri, snapshot_manifest_sha256,
        mlflow_tracking_uri, model,
    )


@dsl.container_component
def train_logistic_regression(
    dataset_version: str, silver_run_id: str, feature_run_id: str,
    snapshot_id: str, snapshot_uri: str, snapshot_manifest_sha256: str,
    mlflow_tracking_uri: str, model: Output[Model],
) -> dsl.ContainerSpec:
    return _train_spec(
        "logistic_regression", dataset_version, silver_run_id, feature_run_id,
        snapshot_id, snapshot_uri, snapshot_manifest_sha256,
        mlflow_tracking_uri, model,
    )


@dsl.container_component
def train_random_forest(
    dataset_version: str, silver_run_id: str, feature_run_id: str,
    snapshot_id: str, snapshot_uri: str, snapshot_manifest_sha256: str,
    mlflow_tracking_uri: str, model: Output[Model],
) -> dsl.ContainerSpec:
    return _train_spec(
        "random_forest", dataset_version, silver_run_id, feature_run_id,
        snapshot_id, snapshot_uri, snapshot_manifest_sha256,
        mlflow_tracking_uri, model,
    )


@dsl.container_component
def train_hist_gradient_boosting(
    dataset_version: str, silver_run_id: str, feature_run_id: str,
    snapshot_id: str, snapshot_uri: str, snapshot_manifest_sha256: str,
    mlflow_tracking_uri: str, model: Output[Model],
) -> dsl.ContainerSpec:
    return _train_spec(
        "hist_gradient_boosting", dataset_version, silver_run_id,
        feature_run_id, snapshot_id, snapshot_uri,
        snapshot_manifest_sha256, mlflow_tracking_uri, model,
    )


def _candidate_arguments(
    dummy_baseline: Input[Model],
    logistic_regression: Input[Model],
    random_forest: Input[Model],
    hist_gradient_boosting: Input[Model],
) -> list[str]:
    return [
        "--dummy-baseline", dummy_baseline.path,
        "--logistic-regression", logistic_regression.path,
        "--random-forest", random_forest.path,
        "--hist-gradient-boosting", hist_gradient_boosting.path,
    ]


@dsl.container_component
def select_validation_winner(
    dummy_baseline: Input[Model],
    logistic_regression: Input[Model],
    random_forest: Input[Model],
    hist_gradient_boosting: Input[Model],
    winner: Output[Artifact],
) -> dsl.ContainerSpec:
    return _runtime_spec(
        "select-winner",
        _candidate_arguments(
            dummy_baseline, logistic_regression, random_forest,
            hist_gradient_boosting,
        ) + ["--output", winner.path],
    )


@dsl.container_component
def evaluate_test_winner(
    dataset_version: str,
    silver_run_id: str,
    feature_run_id: str,
    snapshot_id: str,
    snapshot_uri: str,
    snapshot_manifest_sha256: str,
    mlflow_tracking_uri: str,
    dummy_baseline: Input[Model],
    logistic_regression: Input[Model],
    random_forest: Input[Model],
    hist_gradient_boosting: Input[Model],
    winner: Input[Artifact],
    experiment_result: Output[Artifact],
) -> dsl.ContainerSpec:
    return _runtime_spec(
        "evaluate-winner",
        _common_arguments(
            dataset_version, silver_run_id, feature_run_id, snapshot_id,
            snapshot_uri, snapshot_manifest_sha256, mlflow_tracking_uri,
        ) + _candidate_arguments(
            dummy_baseline, logistic_regression, random_forest,
            hist_gradient_boosting,
        ) + ["--winner", winner.path, "--output", experiment_result.path],
    )


@dsl.container_component
def publish_winner_to_mlflow(
    dataset_version: str,
    silver_run_id: str,
    feature_run_id: str,
    snapshot_id: str,
    snapshot_uri: str,
    snapshot_manifest_sha256: str,
    mlflow_tracking_uri: str,
    experiment_name: str,
    registered_model_name: str,
    git_commit_sha: str,
    experiment_result: Input[Artifact],
    evidence: Output[Artifact],
) -> dsl.ContainerSpec:
    return _runtime_spec(
        "publish-mlflow",
        _common_arguments(
            dataset_version, silver_run_id, feature_run_id, snapshot_id,
            snapshot_uri, snapshot_manifest_sha256, mlflow_tracking_uri,
        ) + [
            "--experiment-result", experiment_result.path,
            "--experiment-name", experiment_name,
            "--registered-model-name", registered_model_name,
            "--git-commit-sha", git_commit_sha,
            "--output", evidence.path,
        ],
    )


def _bounded(task: dsl.PipelineTask, *, training: bool = False) -> dsl.PipelineTask:
    if training:
        return (
            task.set_cpu_request("500m").set_cpu_limit("2")
            .set_memory_request("1Gi").set_memory_limit("3Gi")
            .set_caching_options(False)
        )
    return (
        task.set_cpu_request("100m").set_cpu_limit("500m")
        .set_memory_request("256Mi").set_memory_limit("1Gi")
        .set_caching_options(False)
    )


@dsl.pipeline(
    name="f9-canonical-bounded-experiment",
    description="Validation-only selection and winner-only test/MLflow registration.",
)
def f9_canonical_pipeline(
    dataset_version: str,
    silver_run_id: str,
    feature_run_id: str,
    snapshot_id: str,
    snapshot_uri: str,
    snapshot_manifest_sha256: str,
    mlflow_tracking_uri: str,
    git_commit_sha: str,
    experiment_name: str = "fraud-detection-canonical",
    registered_model_name: str = "fraud-detection-winner",
) -> None:
    common = dict(
        dataset_version=dataset_version,
        silver_run_id=silver_run_id,
        feature_run_id=feature_run_id,
        snapshot_id=snapshot_id,
        snapshot_uri=snapshot_uri,
        snapshot_manifest_sha256=snapshot_manifest_sha256,
        mlflow_tracking_uri=mlflow_tracking_uri,
    )
    snapshot_gate = _bounded(validate_snapshot(**common))
    split_gate = _bounded(verify_chronological_splits(**common))
    split_gate.after(snapshot_gate)

    trainers = [
        _bounded(train_dummy_baseline(**common), training=True),
        _bounded(train_logistic_regression(**common), training=True),
        _bounded(train_random_forest(**common), training=True),
        _bounded(train_hist_gradient_boosting(**common), training=True),
    ]
    for task in trainers:
        task.after(split_gate)

    candidate_outputs = dict(
        dummy_baseline=trainers[0].outputs["model"],
        logistic_regression=trainers[1].outputs["model"],
        random_forest=trainers[2].outputs["model"],
        hist_gradient_boosting=trainers[3].outputs["model"],
    )
    selection = _bounded(select_validation_winner(**candidate_outputs))
    evaluation = _bounded(
        evaluate_test_winner(
            **common,
            **candidate_outputs,
            winner=selection.outputs["winner"],
        ),
        training=True,
    )
    _bounded(
        publish_winner_to_mlflow(
            **common,
            experiment_name=experiment_name,
            registered_model_name=registered_model_name,
            git_commit_sha=git_commit_sha,
            experiment_result=evaluation.outputs["experiment_result"],
        ),
        training=True,
    )
