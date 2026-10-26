"""Run canonical bounded F9 and publish the comparison to MLflow."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ai_platform.ml.modeling.data import load_dataset, prepare_modeling_splits
from ai_platform.ml.modeling.experiment import ExperimentRunner
from ai_platform.ml.tracking.config import load_experiment_config
from ai_platform.ml.tracking.mlflow_tracker import MlflowTracker
from ai_platform.ml.tracking.snapshot import load_snapshot_provenance


def git_commit_sha(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def run(config_path: Path, repo_root: Path) -> dict[str, object]:
    config = load_experiment_config(config_path, repo_root=repo_root)
    snapshot = load_snapshot_provenance(config.dataset_path, config.snapshot)
    dataset = load_dataset(config.dataset_path)
    splits = prepare_modeling_splits(dataset)
    result = ExperimentRunner(config.candidates, seed=config.seed).run(splits)
    provenance = {
        **snapshot.mlflow_params(),
        "git_commit_sha": git_commit_sha(repo_root),
        "random_seed": config.seed,
    }
    published = MlflowTracker(
        tracking_uri=config.tracking.uri,
        experiment_name=config.tracking.experiment_name,
        registered_model_name=config.tracking.registered_model_name,
        registry_mode=config.tracking.registry_mode,
    ).publish(
        result,
        input_example=splits["validation"].X.iloc[:5],
        provenance=provenance,
    )
    evidence = {
        "status": "PASS",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "winner": result.winner.name,
        "test_evaluated_candidates": [
            candidate.name for candidate in result.candidates if candidate.tested
        ],
        "candidate_run_ids": dict(published.candidate_run_ids),
        "experiment_id": published.experiment_id,
        "parent_run_id": published.parent_run_id,
        "winner_model_uri": published.winner_model_uri,
        "registered_model_name": config.tracking.registered_model_name,
        "registered_model_version": published.registered_model_version,
        "production_promotion": "NOT_PERFORMED",
        "provenance": provenance,
    }
    config.output_path.mkdir(parents=True, exist_ok=False)
    (config.output_path / "f9_mlflow_evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return evidence


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("ai_platform/ml/config/f9_mlflow_experiment.yaml"),
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args.config, args.repo_root)
    except Exception as exc:
        print(f"ERROR | {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
