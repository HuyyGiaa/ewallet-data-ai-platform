"""Statistics shared by the A/B experiments; standard library only.

``split_check`` compares observed traffic with the configured split. The quality
functions take labels and scores, so an unlabelled stream can never reach them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class SplitCheck:
    """Observed traffic against the configured split."""

    total: int
    observed_percent: dict[str, float]
    chi_square: float
    critical_value: float
    within_tolerance: bool


def split_check(observed: Mapping[str, int], configured: Mapping[str, int]) -> SplitCheck:
    """Chi-square goodness of fit at p=0.001 for one degree of freedom.

    A zero-weight arm that received traffic always fails, because that is a leak
    rather than sampling noise.
    """
    total = sum(observed.values())
    if total == 0:
        raise ValueError("no traffic was observed")
    leaked = any(weight == 0 and observed.get(arm, 0) for arm, weight in configured.items())
    chi_square = sum(
        (observed.get(arm, 0) - total * weight / 100) ** 2 / (total * weight / 100)
        for arm, weight in configured.items()
        if weight
    )
    critical = 10.828
    return SplitCheck(
        total=total,
        observed_percent={arm: round(100 * observed.get(arm, 0) / total, 2) for arm in configured},
        chi_square=round(chi_square, 3),
        critical_value=critical,
        within_tolerance=not leaked and chi_square <= critical,
    )


def percentile(values: Sequence[float], fraction: float) -> float:
    """Return the nearest-rank percentile of a non-empty sequence."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def average_precision(labels: Sequence[int], scores: Sequence[float]) -> float:
    """Return PR-AUC as average precision, with tied scores handled as one step."""
    positives = sum(labels)
    if positives == 0:
        raise ValueError("average precision needs at least one positive label")
    ranked = sorted(zip(scores, labels), key=lambda item: -item[0])
    true_positive = seen = 0
    area = recall_before = 0.0
    index = 0
    while index < len(ranked):
        score = ranked[index][0]
        while index < len(ranked) and ranked[index][0] == score:
            true_positive += ranked[index][1]
            seen += 1
            index += 1
        recall = true_positive / positives
        area += (recall - recall_before) * (true_positive / seen)
        recall_before = recall
    return area


def classification_report(
    labels: Sequence[int], scores: Sequence[float], predictions: Sequence[int]
) -> dict[str, float | int]:
    """Summarise one model on one fixed labelled set."""
    pairs = list(zip(labels, predictions))
    true_positive = sum(1 for label, value in pairs if label and value)
    false_positive = sum(1 for label, value in pairs if not label and value)
    false_negative = sum(1 for label, value in pairs if label and not value)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    return {
        "rows": len(labels),
        "positives": sum(labels),
        "pr_auc": round(average_precision(labels, scores), 6),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(2 * precision * recall / (precision + recall), 6) if precision + recall else 0.0,
        "false_positives": false_positive,
        "brier": round(sum((s - l) ** 2 for l, s in zip(labels, scores)) / len(labels), 6),
    }
