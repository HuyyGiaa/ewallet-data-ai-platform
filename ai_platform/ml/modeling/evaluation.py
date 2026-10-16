"""Validation-only thresholding and fraud-model evaluation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)


FRAUD_SCENARIOS = (
    "velocity",
    "amount_anomaly",
    "account_takeover",
    "merchant_burst",
)


@dataclass(frozen=True)
class ThresholdResult:
    threshold: float
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True)
class MetricReport:
    pr_auc: float
    roc_auc: float
    precision: float
    recall: float
    f1: float
    confusion_matrix: list[list[int]]

    def as_dict(self) -> dict[str, object]:
        return {
            "pr_auc": self.pr_auc,
            "roc_auc": self.roc_auc,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "confusion_matrix": self.confusion_matrix,
        }


def choose_threshold(y_true: np.ndarray, scores: np.ndarray) -> ThresholdResult:
    """Maximize validation F1, then recall, then prefer lower threshold."""
    y = np.asarray(y_true, dtype=int)
    probabilities = np.asarray(scores, dtype=float)
    precision, recall, thresholds = precision_recall_curve(y, probabilities)
    if not len(thresholds):
        raise ValueError("Cannot select threshold from empty validation scores")
    candidates: list[ThresholdResult] = []
    for index, threshold in enumerate(thresholds):
        denominator = precision[index] + recall[index]
        f1 = 0.0 if denominator == 0 else 2 * precision[index] * recall[index] / denominator
        candidates.append(
            ThresholdResult(
                threshold=float(threshold),
                precision=float(precision[index]),
                recall=float(recall[index]),
                f1=float(f1),
            )
        )
    return max(
        candidates,
        key=lambda item: (item.f1, item.recall, -item.threshold),
    )


def evaluate_scores(
    y_true: np.ndarray, scores: np.ndarray, threshold: float
) -> MetricReport:
    y = np.asarray(y_true, dtype=int)
    probabilities = np.asarray(scores, dtype=float)
    predictions = (probabilities >= threshold).astype(int)
    matrix = confusion_matrix(y, predictions, labels=[0, 1])
    return MetricReport(
        pr_auc=float(average_precision_score(y, probabilities)),
        roc_auc=float(roc_auc_score(y, probabilities)),
        precision=float(precision_score(y, predictions, zero_division=0)),
        recall=float(recall_score(y, predictions, zero_division=0)),
        f1=float(f1_score(y, predictions, zero_division=0)),
        confusion_matrix=[[int(value) for value in row] for row in matrix.tolist()],
    )


def scenario_report(
    y_true: np.ndarray,
    predictions: np.ndarray,
    fraud_types: pd.Series,
) -> dict[str, dict[str, int | float | None]]:
    y = np.asarray(y_true, dtype=int)
    predicted = np.asarray(predictions, dtype=int)
    types = pd.Series(fraud_types).reset_index(drop=True)
    result: dict[str, dict[str, int | float | None]] = {}
    for scenario in FRAUD_SCENARIOS:
        mask = (y == 1) & types.eq(scenario).to_numpy()
        support = int(mask.sum())
        detected = int(((predicted == 1) & mask).sum())
        result[scenario] = {
            "support": support,
            "detected": detected,
            "recall": None if support == 0 else float(detected / support),
        }
    normal = y == 0
    normal_count = int(normal.sum())
    false_positives = int(((predicted == 1) & normal).sum())
    result["normal"] = {
        "support": normal_count,
        "false_positives": false_positives,
        "false_positive_rate": (
            None if normal_count == 0 else float(false_positives / normal_count)
        ),
    }
    return result
