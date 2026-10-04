"""Feast historical retrieval with explicit nullable-merchant handling."""

from __future__ import annotations

import pandas as pd
from feast import FeatureStore

from ai_platform.features.feast import FEATURE_REPO
from ai_platform.features.feast.schema import FEATURE_SPECS


def get_store(repo_path=FEATURE_REPO) -> FeatureStore:
    return FeatureStore(repo_path=str(repo_path))


def normalize_entity_dataframe(entity_df: pd.DataFrame) -> pd.DataFrame:
    result = entity_df.copy()
    result["event_timestamp"] = pd.to_datetime(
        result["event_timestamp"], utc=True
    )
    return result


def feature_refs(view_name: str) -> list[str]:
    return [
        f"{view_name}:{feature}"
        for feature in FEATURE_SPECS[view_name]["features"]
    ]


def retrieve_feature_view(
    store: FeatureStore,
    view_name: str,
    entity_df: pd.DataFrame,
) -> pd.DataFrame:
    entities = normalize_entity_dataframe(entity_df).reset_index(drop=True)
    entities["_f6_row_id"] = range(len(entities))
    entity_key = FEATURE_SPECS[view_name]["entity_key"]
    eligible = (
        entities[entities[entity_key].notna()][[entity_key, "event_timestamp"]]
        .drop_duplicates()
    )
    if eligible.empty:
        result = entities.copy()
        for feature in FEATURE_SPECS[view_name]["features"]:
            result[f"{view_name}__{feature}"] = pd.NA
        return result.drop(columns="_f6_row_id")

    snapshots = store.get_historical_features(
        entity_df=eligible,
        features=feature_refs(view_name),
        full_feature_names=True,
    ).to_df()
    result = entities.merge(
        snapshots,
        on=[entity_key, "event_timestamp"],
        how="left",
        validate="many_to_one",
    )
    return (
        result.sort_values("_f6_row_id")
        .drop(columns="_f6_row_id")
        .reset_index(drop=True)
    )


def retrieve_combined(
    store: FeatureStore,
    entity_df: pd.DataFrame,
) -> pd.DataFrame:
    """Retrieve all views without dropping rows whose merchant is null.

    User, account, and device features apply to every transaction. Merchant
    features apply only to rows with a merchant key, so those rows are fetched
    separately and left-joined back by an internal stable row id.
    """
    result = normalize_entity_dataframe(entity_df).reset_index(drop=True)
    result["_f6_row_id"] = range(len(result))
    for view_name, spec in FEATURE_SPECS.items():
        entity_key = spec["entity_key"]
        eligible = result[result[entity_key].notna()][
            [entity_key, "event_timestamp"]
        ].drop_duplicates()
        feature_columns = [
            f"{view_name}__{feature}" for feature in spec["features"]
        ]
        if eligible.empty:
            for column in feature_columns:
                result[column] = pd.NA
            continue
        snapshots = store.get_historical_features(
            entity_df=eligible,
            features=feature_refs(view_name),
            full_feature_names=True,
        ).to_df()
        result = result.merge(
            snapshots[[entity_key, "event_timestamp", *feature_columns]],
            on=[entity_key, "event_timestamp"],
            how="left",
            validate="many_to_one",
        )
    return (
        result.sort_values("_f6_row_id")
        .drop(columns="_f6_row_id")
        .reset_index(drop=True)
    )
