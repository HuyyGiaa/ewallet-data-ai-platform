"""Candidate estimator construction for the F8 comparison matrix."""

from __future__ import annotations

from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from ai_platform.ml.modeling.config import CandidateConfig
from ai_platform.ml.modeling.features import FeatureStrategy, build_feature_transformer


def _estimator(candidate: CandidateConfig, seed: int):
    params = dict(candidate.params)
    if candidate.estimator == "dummy":
        return DummyClassifier(**params)
    if candidate.estimator == "logistic":
        return LogisticRegression(
            class_weight="balanced",
            random_state=seed,
            solver="lbfgs",
            **params,
        )
    if candidate.estimator == "random_forest":
        return RandomForestClassifier(
            class_weight="balanced",
            random_state=seed,
            **params,
        )
    if candidate.estimator == "hist_gradient_boosting":
        return HistGradientBoostingClassifier(
            class_weight="balanced",
            random_state=seed,
            **params,
        )
    raise ValueError(f"Unsupported estimator: {candidate.estimator}")


def build_candidate(
    candidate: CandidateConfig,
    seed: int,
    selector_c: float = 0.1,
    pca_variance: float = 0.95,
) -> Pipeline:
    """Build one serializable feature-and-estimator pipeline."""
    if candidate.strategy in {"selected", "pca"} and candidate.estimator != "logistic":
        raise ValueError(
            f"Feature strategy {candidate.strategy} requires logistic estimator"
        )
    feature_strategy = FeatureStrategy(
        name=candidate.strategy,
        scale_numeric=candidate.estimator == "logistic",
        selector_c=selector_c,
        pca_variance=pca_variance,
    )
    return Pipeline(
        [
            ("features", build_feature_transformer(feature_strategy, seed)),
            ("estimator", _estimator(candidate, seed)),
        ]
    )
