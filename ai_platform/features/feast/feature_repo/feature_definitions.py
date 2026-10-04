"""Feast entities and FeatureViews for the four F5 Gold feature tables."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from feast import Entity, FeatureView, Field, FileSource, ValueType
from feast.types import Float64, Int64

from ai_platform.features.feast import OFFLINE_DATA_ROOT
from ai_platform.features.feast.schema import FEATURE_SPECS


def build_feature_objects(data_root: str | Path | None = None):
    """Build definitions against a selectable Parquet snapshot root."""
    root = Path(
        data_root
        or os.getenv("EWALLET_FEAST_OFFLINE_DATA_ROOT", str(OFFLINE_DATA_ROOT))
    ).resolve()

    entities = {}
    views = {}
    sources = {}
    for view_name, spec in FEATURE_SPECS.items():
        entity = Entity(
            name=spec["entity_name"],
            join_keys=[spec["entity_key"]],
            value_type=ValueType.STRING,
            description=f"E-Wallet {spec['entity_name']} entity",
        )
        source = FileSource(
            name=f"{view_name}_source",
            path=str(root / spec["table"]),
            timestamp_field="event_timestamp",
        )
        integer_features = set(spec["integer_features"])
        view = FeatureView(
            name=view_name,
            entities=[entity],
            ttl=timedelta(days=3650),
            schema=[
                Field(
                    name=feature,
                    dtype=Int64 if feature in integer_features else Float64,
                )
                for feature in spec["features"]
            ],
            source=source,
            online=False,
            enable_validation=True,
            tags={
                "layer": "gold",
                "temporal_contract": "[T-window,T)",
                "contains_labels": "false",
            },
        )
        entities[spec["entity_name"]] = entity
        sources[view_name] = source
        views[view_name] = view
    return entities, sources, views


_entities, _sources, _views = build_feature_objects()

user = _entities["user"]
account = _entities["account"]
device = _entities["device"]
merchant = _entities["merchant"]

user_behavior_source = _sources["user_behavior"]
account_behavior_source = _sources["account_behavior"]
device_behavior_source = _sources["device_behavior"]
merchant_behavior_source = _sources["merchant_behavior"]

user_behavior = _views["user_behavior"]
account_behavior = _views["account_behavior"]
device_behavior = _views["device_behavior"]
merchant_behavior = _views["merchant_behavior"]
