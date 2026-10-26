"""Deterministic validation-only model comparison for fraud detection."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Mapping, Protocol, Sequence

from sklearn.pipeline import Pipeline

from ai_platform.ml.modeling.config import ModelConfig
from ai_platform.ml.modeling.data import ModelingSplit
from ai_platform.ml.modeling.evaluation import (
    choose_threshold,
    evaluate_scores,
    scenario_report,
)
from ai_platform.ml.modeling.models import build_candidate_model


class CandidateDefinition(Protocol):
    model: ModelConfig
    selection_eligible: bool


@dataclass(frozen=True)
class CandidateResult:
    name: str
    estimator: str
    pipeline: Pipeline
    threshold: float
    validation_report: dict[str, object]
    selection_eligible: bool
    tested: bool = False


@dataclass(frozen=True)
class ExperimentResult:
    candidates: tuple[CandidateResult, ...]
    winner: CandidateResult
    test_report: dict[str, object]


class ExperimentRunner:
    """Fit all candidates on train, select on validation, test one winner."""

    def __init__(
        self, candidates: Sequence[CandidateDefinition], *, seed: int
    ) -> None:
        if not candidates:
            raise ValueError("At least one model candidate is required")
        names = [candidate.model.name for candidate in candidates]
        if len(names) != len(set(names)):
            raise ValueError("Candidate names must be unique")
        if not any(candidate.selection_eligible for candidate in candidates):
            raise ValueError("At least one candidate must be selection eligible")
        self._candidates = tuple(candidates)
        self._seed = seed

    def run(self, splits: Mapping[str, ModelingSplit]) -> ExperimentResult:
        required = {"train", "validation", "test"}
        if set(splits) != required:
            raise ValueError(f"Experiment splits must be exactly {sorted(required)}")
        results = tuple(
            train_candidate(
                candidate,
                splits["train"],
                splits["validation"],
                seed=self._seed,
            )
            for candidate in self._candidates
        )
        winner = select_validation_winner(results)
        return evaluate_test_winner(results, winner.name, splits["test"])


def train_candidate(
    candidate: CandidateDefinition,
    train: ModelingSplit,
    validation: ModelingSplit,
    *,
    seed: int,
) -> CandidateResult:
    """Fit one configured candidate and evaluate validation only."""
    config = candidate.model
    pipeline = build_candidate_model(config, seed)
    fit_started = time.perf_counter()
    pipeline.fit(train.X, train.y)
    fit_seconds = time.perf_counter() - fit_started
    predict_started = time.perf_counter()
    scores = pipeline.predict_proba(validation.X)[:, 1]
    predict_seconds = time.perf_counter() - predict_started
    selected = choose_threshold(validation.y.to_numpy(), scores)
    metrics = evaluate_scores(validation.y.to_numpy(), scores, selected.threshold)
    return CandidateResult(
        name=config.name,
        estimator=config.estimator,
        pipeline=pipeline,
        threshold=selected.threshold,
        selection_eligible=candidate.selection_eligible,
        validation_report={
            "model": config.name,
            "rows": int(len(validation.y)),
            "threshold": selected.threshold,
            "metrics": metrics.as_dict(),
            "fit_seconds": float(fit_seconds),
            "predict_seconds": float(predict_seconds),
            "parameters": dict(config.params),
        },
    )


def select_validation_winner(
    results: Sequence[CandidateResult],
) -> CandidateResult:
    """Select from eligible validation results without reading test data."""
    pool = [result for result in results if result.selection_eligible]
    if not pool:
        raise ValueError("At least one validation result must be selection eligible")
    return max(
        pool,
        key=lambda result: (
            float(result.validation_report["metrics"]["pr_auc"]),
            float(result.validation_report["metrics"]["f1"]),
            result.name,
        ),
    )


def evaluate_test_winner(
    results: Sequence[CandidateResult],
    winner_name: str,
    test: ModelingSplit,
) -> ExperimentResult:
    """Evaluate exactly one preselected winner against the held-out test split."""
    selected = [result for result in results if result.name == winner_name]
    if len(selected) != 1:
        raise ValueError(f"Expected one selected winner named {winner_name!r}")
    winner = selected[0]
    eligible_winner = select_validation_winner(results)
    if winner.name != eligible_winner.name:
        raise ValueError(
            f"Requested winner {winner.name!r} differs from validation winner "
            f"{eligible_winner.name!r}"
        )
    test_started = time.perf_counter()
    test_scores = winner.pipeline.predict_proba(test.X)[:, 1]
    test_seconds = time.perf_counter() - test_started
    test_metrics = evaluate_scores(test.y.to_numpy(), test_scores, winner.threshold)
    predictions = (test_scores >= winner.threshold).astype(int)
    test_report: dict[str, object] = {
        "model": winner.name,
        "rows": int(len(test.y)),
        "threshold": winner.threshold,
        "metrics": test_metrics.as_dict(),
        "scenarios": scenario_report(
            test.y.to_numpy(), predictions, test.diagnostics["fraud_type"]
        ),
        "predict_seconds": float(test_seconds),
    }
    finalized = tuple(
        replace(result, tested=result.name == winner.name) for result in results
    )
    final_winner = next(result for result in finalized if result.tested)
    return ExperimentResult(finalized, final_winner, test_report)
