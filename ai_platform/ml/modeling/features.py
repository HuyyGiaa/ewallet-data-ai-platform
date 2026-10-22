"""Leakage-safe preprocessing for the F8 Random Forest baseline."""

from __future__ import annotations

from sklearn.compose import ColumnTransformer
from sklearn.feature_selection import VarianceThreshold
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from ai_platform.ml.training.contract import MODEL_FEATURE_COLUMNS


CATEGORICAL_COLUMNS = ("type", "channel")
NUMERIC_COLUMNS = tuple(
    column for column in MODEL_FEATURE_COLUMNS if column not in CATEGORICAL_COLUMNS
)


def _numeric_pipeline(*, scale: bool = False) -> Pipeline:
    steps = [("imputer", SimpleImputer(strategy="median", add_indicator=True))]
    if scale:
        steps.append(("scale", StandardScaler()))
    return Pipeline(steps)


def _categorical_pipeline() -> Pipeline:
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="most_frequent")),
            (
                "one_hot",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
            ),
        ]
    )


def build_feature_transformer(*, scale_numeric: bool = False) -> Pipeline:
    """Build one train-fitted representation for leakage-safe candidates."""
    preprocess = ColumnTransformer(
        [
            ("numeric", _numeric_pipeline(scale=scale_numeric), NUMERIC_COLUMNS),
            ("categorical", _categorical_pipeline(), CATEGORICAL_COLUMNS),
        ],
        sparse_threshold=0.0,
        verbose_feature_names_out=True,
    )
    return Pipeline(
        [
            ("preprocess", preprocess),
            ("variance", VarianceThreshold()),
        ]
    )


def transformed_feature_summary(fitted: Pipeline) -> dict[str, object]:
    names = fitted.get_feature_names_out().tolist()
    return {
        "input_feature_count": len(MODEL_FEATURE_COLUMNS),
        "output_feature_count": len(names),
        "output_feature_names": names,
    }
