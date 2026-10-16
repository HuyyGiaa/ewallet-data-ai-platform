"""Random Forest construction for the active F8 baseline."""

from __future__ import annotations

from sklearn.ensemble import RandomForestClassifier
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
