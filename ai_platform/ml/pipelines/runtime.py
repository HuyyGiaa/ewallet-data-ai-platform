"""Container runtime commands for the canonical standalone KFP workflow."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urlparse

import joblib
from google.cloud import storage

from ai_platform.ml.modeling.data import load_dataset, prepare_modeling_splits
from ai_platform.ml.modeling.experiment import (
    CandidateResult,
    ExperimentResult,
    evaluate_test_winner,
    select_validation_winner,
    train_candidate,
)
from ai_platform.ml.tracking.config import ExperimentConfig, load_experiment_config
from ai_platform.ml.tracking.mlflow_tracker import MlflowTracker
from ai_platform.ml.tracking.snapshot import load_snapshot_provenance
from ai_platform.ml.training.temporal import assign_temporal_split


CANDIDATE_NAMES = (
    "dummy_baseline",
    "logistic_regression",
    "random_forest",
    "hist_gradient_boosting",
)


def _gcs_location(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError(f"Expected immutable gs:// snapshot URI, got {uri!r}")
    return parsed.netloc, parsed.path.strip("/")


def _download_manifest(uri: str, target_root: Path) -> Path:
    bucket_name, prefix = _gcs_location(uri)
    target_root.mkdir(parents=True, exist_ok=False)
    target = target_root / "manifest.json"
    storage.Client().bucket(bucket_name).blob(
        f"{prefix.rstrip('/')}/manifest.json"
    ).download_to_filename(target)
    return target


def _download_snapshot(uri: str, target_root: Path) -> Path:
    bucket_name, prefix = _gcs_location(uri)
    target_root.mkdir(parents=True, exist_ok=False)
    prefix = prefix.rstrip("/") + "/"
    count = 0
    for blob in storage.Client().list_blobs(bucket_name, prefix=prefix):
        relative = PurePosixPath(blob.name.removeprefix(prefix))
        if not relative.parts or ".." in relative.parts or blob.name.endswith("/"):
            continue
        target = target_root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(target)
        count += 1
    if count == 0 or not (target_root / "manifest.json").is_file():
        raise FileNotFoundError(f"Snapshot has no downloadable manifest: {uri}")
    return target_root


def _environment(args: argparse.Namespace, dataset_path: Path) -> dict[str, str]:
    return {
        "F9_DATASET_PATH": str(dataset_path),
        "F9_OUTPUT_PATH": str(dataset_path.parent / "unused-output"),
        "F9_TRAINING_SNAPSHOT_URI": args.snapshot_uri,
        "DATASET_VERSION": args.dataset_version,
        "SILVER_RUN_ID": args.silver_run_id,
        "FEATURE_RUN_ID": args.feature_run_id,
        "SNAPSHOT_ID": args.snapshot_id,
        "SNAPSHOT_MANIFEST_SHA256": args.snapshot_manifest_sha256,
        "MLFLOW_TRACKING_URI": args.mlflow_tracking_uri,
        "MLFLOW_EXPERIMENT_NAME": args.experiment_name,
        "MLFLOW_REGISTERED_MODEL_NAME": args.registered_model_name,
    }


def _config(args: argparse.Namespace, dataset_path: Path) -> ExperimentConfig:
    return load_experiment_config(
        Path(args.config),
        repo_root=Path(args.repo_root),
        environment=_environment(args, dataset_path),
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _candidate_results(paths: Iterable[Path]) -> tuple[CandidateResult, ...]:
    results = tuple(joblib.load(path) for path in paths)
    names = tuple(result.name for result in results)
    if names != CANDIDATE_NAMES:
        raise ValueError(f"Candidate artifact order {names} != {CANDIDATE_NAMES}")
    if any(result.tested for result in results):
        raise ValueError("Candidate artifacts must not contain test evaluation")
    return results


def _validate_manifest(
    args: argparse.Namespace, dataset_path: Path
) -> tuple[ExperimentConfig, object]:
    config = _config(args, dataset_path)
    provenance = load_snapshot_provenance(dataset_path, config.snapshot)
    if provenance.manifest_sha256 != args.snapshot_manifest_sha256:
        raise ValueError(
            "Snapshot manifest checksum mismatch: "
            f"{provenance.manifest_sha256} != {args.snapshot_manifest_sha256}"
        )
    return config, provenance


def validate_snapshot(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="f9-manifest-") as temporary:
        root = Path(temporary) / "snapshot"
        _download_manifest(args.snapshot_uri, root)
        _, provenance = _validate_manifest(args, root)
        _write_json(Path(args.output), provenance.mlflow_params())
        print(json.dumps(provenance.mlflow_params(), sort_keys=True))


def verify_chronological_splits(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="f9-splits-") as temporary:
        root = _download_snapshot(args.snapshot_uri, Path(temporary) / "snapshot")
        _, provenance = _validate_manifest(args, root)
        dataset = load_dataset(root)
        actual_rows = 0
        actual_positive = 0
        for split in ("train", "validation", "test"):
            frame = getattr(dataset, split)
            if len(frame) != provenance.split_counts[split]:
                raise ValueError(
                    f"Actual {split} rows {len(frame)} != manifest "
                    f"{provenance.split_counts[split]}"
                )
            actual_rows += len(frame)
            actual_positive += int(frame["label"].sum())
            expected = assign_temporal_split(frame["event_timestamp"])
            if not expected.eq(split).all():
                raise ValueError(f"Chronological split contract failed for {split}")
        if actual_rows != provenance.row_count:
            raise ValueError(
                f"Actual rows {actual_rows} != manifest {provenance.row_count}"
            )
        if actual_positive != provenance.positive_count:
            raise ValueError(
                "Actual positive class count "
                f"{actual_positive} != manifest {provenance.positive_count}"
            )
        if actual_rows - actual_positive != provenance.negative_count:
            raise ValueError("Actual negative class count does not match manifest")
        _write_json(
            Path(args.output),
            {
                "status": "PASS",
                "split_counts": provenance.split_counts,
                "transaction_id_overlap": 0,
                "split_assignment": "chronological",
            },
        )


def train_one_candidate(args: argparse.Namespace) -> None:
    if args.candidate not in CANDIDATE_NAMES:
        raise ValueError(f"Unsupported canonical candidate {args.candidate!r}")
    with tempfile.TemporaryDirectory(prefix=f"f9-{args.candidate}-") as temporary:
        root = _download_snapshot(args.snapshot_uri, Path(temporary) / "snapshot")
        config, _ = _validate_manifest(args, root)
        candidate = next(
            item for item in config.candidates if item.model.name == args.candidate
        )
        splits = prepare_modeling_splits(load_dataset(root))
        result = train_candidate(
            candidate, splits["train"], splits["validation"], seed=config.seed
        )
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(result, args.output)
        print(json.dumps(result.validation_report, sort_keys=True))


def select_winner(args: argparse.Namespace) -> None:
    results = _candidate_results(_candidate_paths(args))
    winner = select_validation_winner(results)
    _write_json(
        Path(args.output),
        {
            "winner": winner.name,
            "primary_metric": "validation_pr_auc",
            "tie_breaker_metric": "validation_f1",
        },
    )


def evaluate_winner(args: argparse.Namespace) -> None:
    results = _candidate_results(_candidate_paths(args))
    winner_payload = json.loads(Path(args.winner).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="f9-test-") as temporary:
        root = _download_snapshot(args.snapshot_uri, Path(temporary) / "snapshot")
        _validate_manifest(args, root)
        splits = prepare_modeling_splits(load_dataset(root))
        experiment = evaluate_test_winner(
            results, winner_payload["winner"], splits["test"]
        )
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(experiment, args.output)
        print(json.dumps(experiment.test_report, sort_keys=True))


def publish_to_mlflow(args: argparse.Namespace) -> None:
    experiment: ExperimentResult = joblib.load(args.experiment_result)
    with tempfile.TemporaryDirectory(prefix="f9-publish-") as temporary:
        root = _download_snapshot(args.snapshot_uri, Path(temporary) / "snapshot")
        config, provenance = _validate_manifest(args, root)
        splits = prepare_modeling_splits(load_dataset(root))
        published = MlflowTracker(
            tracking_uri=args.mlflow_tracking_uri,
            experiment_name=args.experiment_name,
            registered_model_name=args.registered_model_name,
            registry_mode="winner_only",
        ).publish(
            experiment,
            input_example=splits["validation"].X.iloc[:5],
            provenance={
                **provenance.mlflow_params(),
                "git_commit_sha": args.git_commit_sha,
                "random_seed": config.seed,
                "orchestrator": "kfp-standalone",
                "kfp_version": "2.17.3",
                "runtime_image": args.runtime_image,
            },
        )
        _write_json(
            Path(args.output),
            {
                "status": "PASS",
                "winner": experiment.winner.name,
                "test_evaluated_candidates": [
                    candidate.name
                    for candidate in experiment.candidates
                    if candidate.tested
                ],
                "experiment_id": published.experiment_id,
                "parent_run_id": published.parent_run_id,
                "candidate_run_ids": dict(published.candidate_run_ids),
                "registered_model_name": args.registered_model_name,
                "registered_model_version": published.registered_model_version,
                "production_promotion": "NOT_PERFORMED",
                "snapshot_id": args.snapshot_id,
                "git_commit_sha": args.git_commit_sha,
            },
        )


def _candidate_paths(args: argparse.Namespace) -> tuple[Path, ...]:
    return tuple(Path(getattr(args, name)) for name in CANDIDATE_NAMES)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--silver-run-id", required=True)
    parser.add_argument("--feature-run-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--snapshot-uri", required=True)
    parser.add_argument("--snapshot-manifest-sha256", required=True)
    parser.add_argument("--mlflow-tracking-uri", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--compiled-runtime-image", required=True)
    parser.add_argument("--experiment-name", required=True)
    parser.add_argument("--registered-model-name", required=True)
    parser.add_argument(
        "--config", default="/workspace/ai_platform/ml/config/f9_mlflow_experiment.yaml"
    )
    parser.add_argument("--repo-root", default="/workspace")


def _add_candidates(parser: argparse.ArgumentParser) -> None:
    for name in CANDIDATE_NAMES:
        parser.add_argument(f"--{name.replace('_', '-')}", dest=name, required=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate-snapshot", "verify-chronological-splits"):
        child = subparsers.add_parser(command)
        _add_common(child)
        child.add_argument("--output", required=True)
    train = subparsers.add_parser("train-candidate")
    _add_common(train)
    train.add_argument("--candidate", required=True)
    train.add_argument("--output", required=True)
    select = subparsers.add_parser("select-winner")
    _add_candidates(select)
    select.add_argument("--output", required=True)
    evaluate = subparsers.add_parser("evaluate-winner")
    _add_common(evaluate)
    _add_candidates(evaluate)
    evaluate.add_argument("--winner", required=True)
    evaluate.add_argument("--output", required=True)
    publish = subparsers.add_parser("publish-mlflow")
    _add_common(publish)
    publish.add_argument("--experiment-result", required=True)
    publish.add_argument("--git-commit-sha", required=True)
    publish.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    actions = {
        "validate-snapshot": validate_snapshot,
        "verify-chronological-splits": verify_chronological_splits,
        "train-candidate": train_one_candidate,
        "select-winner": select_winner,
        "evaluate-winner": evaluate_winner,
        "publish-mlflow": publish_to_mlflow,
    }
    try:
        if hasattr(args, "compiled_runtime_image") and (
            args.runtime_image != args.compiled_runtime_image
        ):
            raise ValueError(
                "runtime_image input does not match the compiled component image"
            )
        actions[args.command](args)
    except Exception as exc:
        print(f"ERROR | {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
