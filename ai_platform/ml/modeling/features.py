"""Leakage-safe preprocessing and feature representation strategies."""

from __future__ import annotations

from dataclasses import dataclass

from sklearn.compose import ColumnTransformer
from sklearn.decomposition import PCA
from sklearn.feature_selection import SelectFromModel, VarianceThreshold
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from ai_platform.ml.training.contract import MODEL_FEATURE_COLUMNS


CATEGORICAL_COLUMNS = ("type", "channel")
NUMERIC_COLUMNS = tuple(
    column for column in MODEL_FEATURE_COLUMNS if column not in CATEGORICAL_COLUMNS
)


@dataclass(frozen=True)
class FeatureStrategy:
    name: str
    scale_numeric: bool
    selector_c: float
    pca_variance: float


def _numeric_pipeline(
    scale_numeric: bool, pca_variance: float | None = None
) -> Pipeline:
    steps = [("imputer", SimpleImputer(strategy="median", add_indicator=True))]
    if scale_numeric:
        steps.append(("scaler", StandardScaler()))
    if pca_variance is not None:
        steps.append(("pca", PCA(n_components=pca_variance, svd_solver="full")))
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


def build_feature_transformer(strategy: FeatureStrategy, seed: int) -> Pipeline:
    """Build a train-fitted transformer for one configured representation."""
    if strategy.name not in {"full", "selected", "pca"}:
        raise ValueError(f"Unsupported feature strategy: {strategy.name}")
    pca_variance = strategy.pca_variance if strategy.name == "pca" else None
    preprocess = ColumnTransformer(
        [
            (
                "numeric",
                _numeric_pipeline(strategy.scale_numeric, pca_variance),
                NUMERIC_COLUMNS,
            ),
            ("categorical", _categorical_pipeline(), CATEGORICAL_COLUMNS),
        ],
        sparse_threshold=0.0,
        verbose_feature_names_out=True,
    )
    steps = [
        ("preprocess", preprocess),
        ("variance", VarianceThreshold()),
    ]
    if strategy.name == "selected":
        steps.append(
            (
                "selector",
                SelectFromModel(
                    LogisticRegression(
                        C=strategy.selector_c,
                        penalty="l1",
                        solver="liblinear",
                        class_weight="balanced",
                        random_state=seed,
                        max_iter=1000,
                    )
                ),
            )
        )
    return Pipeline(steps)


def transformed_feature_summary(fitted: Pipeline) -> dict[str, object]:
    names = fitted.get_feature_names_out().tolist()
    summary: dict[str, object] = {
        "output_feature_count": len(names),
        "output_feature_names": names,
    }
    if "selector" in fitted.named_steps:
        summary["selected_feature_names"] = names
    numeric = fitted.named_steps["preprocess"].named_transformers_["numeric"]
    if "pca" in numeric.named_steps:
        pca = numeric.named_steps["pca"]
        summary["pca_component_count"] = int(pca.n_components_)
        summary["pca_explained_variance"] = float(
            pca.explained_variance_ratio_.sum()
        )
    return summary
