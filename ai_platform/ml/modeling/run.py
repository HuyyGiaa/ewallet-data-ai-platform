"""Run the complete F8 local model comparison from one command."""

from __future__ import annotations

import argparse
import gc
import hashlib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ai_platform.ml.modeling.artifacts import ArtifactPayload, publish_artifacts
from ai_platform.ml.modeling.config import CandidateConfig, F8Config, load_config
from ai_platform.ml.modeling.data import load_dataset, prepare_modeling_splits
from ai_platform.ml.modeling.evaluation import (
    CandidateResult,
    choose_threshold,
    evaluate_scores,
    scenario_report,
    select_champion,
)
from ai_platform.ml.modeling.features import transformed_feature_summary
from ai_platform.ml.modeling.models import build_candidate


@dataclass(frozen=True)
class RunResult:
    comparison: tuple[CandidateResult, ...]
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


def _pipeline(config: F8Config, candidate: CandidateConfig):
    return build_candidate(
        candidate,
        seed=config.seed,
        selector_c=config.selector_c,
        pca_variance=config.pca_variance,
    )


def _dataset_summary(modeling_splits) -> dict[str, dict[str, float | int]]:
    return {
        name: {
            "rows": int(len(split.y)),
            "fraud": int(split.y.sum()),
            "fraud_rate": float(split.y.mean()),
        }
        for name, split in modeling_splits.items()
    }


def _print_results(results: tuple[CandidateResult, ...], test_report: dict) -> None:
    print("\nF8 local fraud model comparison")
    print("=" * 100)
    print(
        f"{'Experiment':34} {'Features':>8} {'PR-AUC':>9} {'Precision':>10} "
        f"{'Recall':>9} {'F1':>9} {'Fit(s)':>9}"
    )
    for result in results:
        metrics = result.validation_metrics
        print(
            f"{result.name:34} "
            f"{result.feature_summary['output_feature_count']:8d} "
            f"{metrics['pr_auc']:9.6f} {metrics['precision']:10.6f} "
            f"{metrics['recall']:9.6f} {metrics['f1']:9.6f} "
            f"{result.fit_seconds:9.3f}"
        )
    print("-" * 100)
    print(
        f"Champion: {test_report['champion']} | threshold={test_report['threshold']:.6f} "
        f"| test PR-AUC={test_report['metrics']['pr_auc']:.6f} "
        f"| test F1={test_report['metrics']['f1']:.6f}"
    )


def run_comparison(config: F8Config) -> RunResult:
    """Fit all candidates, rank on validation, and test one champion."""
    started = time.perf_counter()
    dataset = load_dataset(config.dataset_path)
    splits = prepare_modeling_splits(dataset)
    train = splits["train"]
    validation = splits["validation"]
    test = splits["test"]

    results: list[CandidateResult] = []
    feature_reports: dict[str, object] = {}
    for candidate in config.candidates:
        pipeline = _pipeline(config, candidate)
        fit_started = time.perf_counter()
        pipeline.fit(train.X, train.y)
        fit_seconds = time.perf_counter() - fit_started
        predict_started = time.perf_counter()
        validation_scores = pipeline.predict_proba(validation.X)[:, 1]
        predict_seconds = time.perf_counter() - predict_started
        threshold = choose_threshold(validation.y.to_numpy(), validation_scores)
        metrics = evaluate_scores(
            validation.y.to_numpy(), validation_scores, threshold.threshold
        )
        feature_summary = transformed_feature_summary(
            pipeline.named_steps["features"]
        )
        feature_summary["strategy"] = candidate.strategy
        result = CandidateResult(
            name=candidate.name,
            threshold=threshold.threshold,
            validation_metrics=metrics.as_dict(),
            feature_summary=feature_summary,
            fit_seconds=float(fit_seconds),
            predict_seconds=float(predict_seconds),
        )
        results.append(result)
        feature_reports[candidate.name] = feature_summary
        del pipeline, validation_scores
        gc.collect()

    configured_order = [candidate.name for candidate in config.candidates]
    champion_result = select_champion(results, configured_order)
    champion_config = next(
        candidate
        for candidate in config.candidates
        if candidate.name == champion_result.name
    )
    champion_pipeline = _pipeline(config, champion_config)
    champion_pipeline.fit(train.X, train.y)
    test_scores = champion_pipeline.predict_proba(test.X)[:, 1]
    test_metrics = evaluate_scores(
        test.y.to_numpy(), test_scores, champion_result.threshold
    )
    test_predictions = (test_scores >= champion_result.threshold).astype(int)
    scenarios = scenario_report(
        test.y.to_numpy(), test_predictions, test.diagnostics["fraud_type"]
    )
    test_report: dict[str, object] = {
        "champion": champion_result.name,
        "threshold": champion_result.threshold,
        "metrics": test_metrics.as_dict(),
        "scenarios": scenarios,
        "rows": int(len(test.y)),
    }
    total_seconds = time.perf_counter() - started
    comparison = tuple(results)
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_manifest_sha256": _input_fingerprint(config.dataset_path),
        "dataset": _dataset_summary(splits),
        "seed": config.seed,
        "candidate_order": configured_order,
        "champion": champion_result.name,
        "total_seconds": float(total_seconds),
    }
    output = publish_artifacts(
        ArtifactPayload(
            comparison=[result.as_dict() for result in comparison],
            champion_pipeline=champion_pipeline,
            selected_features=feature_reports,
            test_report=test_report,
            run_manifest=manifest,
        ),
        config.output_path,
    )
    _print_results(comparison, test_report)
    return RunResult(comparison, test_report, output, total_seconds)


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
        run_comparison(config)
        return 0
    except Exception as exc:
        print(f"ERROR | {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
