"""Train and evaluate the active F8 Random Forest from one command."""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ai_platform.ml.modeling.artifacts import ArtifactPayload, publish_artifacts
from ai_platform.ml.modeling.config import F8Config, load_config
from ai_platform.ml.modeling.data import load_dataset, prepare_modeling_splits
from ai_platform.ml.modeling.evaluation import (
    choose_threshold,
    evaluate_scores,
    scenario_report,
)
from ai_platform.ml.modeling.features import transformed_feature_summary
from ai_platform.ml.modeling.models import build_model


@dataclass(frozen=True)
class RunResult:
    validation_report: dict[str, object]
    test_report: dict[str, object]
    output_path: Path
    total_seconds: float


def _input_fingerprint(dataset_path: Path) -> str:
    digest = hashlib.sha256()
    manifest = dataset_path / "manifest.json"
    paths = [manifest] if manifest.is_file() else sorted(dataset_path.rglob("*.parquet"))
    if not paths:
        raise FileNotFoundError(f"No manifest or Parquet input under {dataset_path}")
    for path in paths:
        digest.update(path.relative_to(dataset_path).as_posix().encode("utf-8"))
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _dataset_summary(modeling_splits) -> dict[str, dict[str, float | int]]:
    return {
        name: {
            "rows": int(len(split.y)),
            "fraud": int(split.y.sum()),
            "fraud_rate": float(split.y.mean()),
        }
        for name, split in modeling_splits.items()
    }


def _print_results(validation_report: dict, test_report: dict) -> None:
    validation = validation_report["metrics"]
    test = test_report["metrics"]
    print("\nF8 Random Forest fraud baseline")
    print("=" * 72)
    print(f"Model: {validation_report['model']}")
    print(
        f"Validation: PR-AUC={validation['pr_auc']:.6f} "
        f"F1={validation['f1']:.6f} threshold={validation_report['threshold']:.6f}"
    )
    print(f"Test:       PR-AUC={test['pr_auc']:.6f} F1={test['f1']:.6f}")


def run_training(config: F8Config) -> RunResult:
    """Fit one Random Forest, select its threshold, and evaluate it once."""
    started = time.perf_counter()
    dataset = load_dataset(config.dataset_path)
    splits = prepare_modeling_splits(dataset)
    train = splits["train"]
    validation = splits["validation"]
    test = splits["test"]

    pipeline = build_model(config.model, seed=config.seed)
    fit_started = time.perf_counter()
    pipeline.fit(train.X, train.y)
    fit_seconds = time.perf_counter() - fit_started

    validation_predict_started = time.perf_counter()
    validation_scores = pipeline.predict_proba(validation.X)[:, 1]
    validation_predict_seconds = time.perf_counter() - validation_predict_started
    threshold = choose_threshold(validation.y.to_numpy(), validation_scores)
    validation_metrics = evaluate_scores(
        validation.y.to_numpy(), validation_scores, threshold.threshold
    )
    feature_summary = transformed_feature_summary(pipeline.named_steps["features"])
    feature_summary["strategy"] = config.model.strategy
    validation_report: dict[str, object] = {
        "model": config.model.name,
        "rows": int(len(validation.y)),
        "threshold": threshold.threshold,
        "metrics": validation_metrics.as_dict(),
        "fit_seconds": float(fit_seconds),
        "predict_seconds": float(validation_predict_seconds),
    }

    test_predict_started = time.perf_counter()
    test_scores = pipeline.predict_proba(test.X)[:, 1]
    test_predict_seconds = time.perf_counter() - test_predict_started
    test_metrics = evaluate_scores(test.y.to_numpy(), test_scores, threshold.threshold)
    test_predictions = (test_scores >= threshold.threshold).astype(int)
    test_report: dict[str, object] = {
        "model": config.model.name,
        "rows": int(len(test.y)),
        "threshold": threshold.threshold,
        "metrics": test_metrics.as_dict(),
        "scenarios": scenario_report(
            test.y.to_numpy(), test_predictions, test.diagnostics["fraud_type"]
        ),
        "predict_seconds": float(test_predict_seconds),
    }

    total_seconds = time.perf_counter() - started
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_manifest_sha256": _input_fingerprint(config.dataset_path),
        "dataset": _dataset_summary(splits),
        "seed": config.seed,
        "model": config.model.name,
        "total_seconds": float(total_seconds),
        "configuration": {
            "seed": config.seed,
            "model": {
                "name": config.model.name,
                "estimator": config.model.estimator,
                "strategy": config.model.strategy,
                "params": config.model.params,
            },
        },
    }
    output = publish_artifacts(
        ArtifactPayload(
            model_pipeline=pipeline,
            validation_report=validation_report,
            test_report=test_report,
            feature_summary=feature_summary,
            run_manifest=manifest,
        ),
        config.output_path,
    )
    _print_results(validation_report, test_report)
    return RunResult(validation_report, test_report, output, total_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("ai_platform/ml/config/f8_baseline.yaml"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config, repo_root=Path.cwd())
        run_training(config)
        return 0
    except Exception as exc:
        print(f"ERROR | {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
