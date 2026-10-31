"""Model construction shared by the F8 fallback and F9 comparison."""

from __future__ import annotations

from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from ai_platform.ml.modeling.config import ModelConfig
from ai_platform.ml.modeling.features import build_feature_transformer


def build_model(model: ModelConfig, seed: int) -> Pipeline:
    """Build the serializable preprocessing-and-model pipeline."""
    return Pipeline(
        [
            ("features", build_feature_transformer()),
            (
                "estimator",
                RandomForestClassifier(
                    class_weight="balanced",
                    random_state=seed,
                    **model.params,
                ),
            ),
        ]
    )


def build_candidate_model(model: ModelConfig, seed: int) -> Pipeline:
    """Build a deterministic F9 candidate with preprocessing in its pipeline."""
    if model.strategy != "full":
        raise ValueError(f"Unsupported feature strategy: {model.strategy}")
    parameters = dict(model.params)
    if model.estimator == "dummy":
        estimator = DummyClassifier(**parameters)
        scale_numeric = False
    elif model.estimator == "logistic_regression":
        estimator = LogisticRegression(
            class_weight="balanced", random_state=seed, **parameters
        )
        scale_numeric = True
    elif model.estimator == "random_forest":
        estimator = RandomForestClassifier(
            class_weight="balanced", random_state=seed, **parameters
        )
        scale_numeric = False
    elif model.estimator == "hist_gradient_boosting":
        estimator = HistGradientBoostingClassifier(
            class_weight="balanced", random_state=seed, **parameters
        )
        scale_numeric = False
    else:
        raise ValueError(f"Unsupported estimator: {model.estimator}")
    return Pipeline(
        [
            ("features", build_feature_transformer(scale_numeric=scale_numeric)),
            ("estimator", estimator),
        ]
    )
