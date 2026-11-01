"""Online-compatible mirror of the immutable four-view F5/DP3 schema.

This lives in a separate Feast repository so it cannot collide with the
offline-only definitions during `feast apply`. PushSource retains the DP3
batch source for materialization and supplies the supported streaming write
path used by :class:`FeastPushWriter`.
"""

from __future__ import annotations

import os
from datetime import timedelta

from feast import Entity, FeatureView, Field, FileSource, PushSource, ValueType
from feast.types import Float64, Int64

from ai_platform.features.feast.repository import join_data_root
from ai_platform.features.feast.schema import FEATURE_SPECS


def build_online_feature_objects(data_root: str | None = None):
    root = data_root or os.environ["EWALLET_FEAST_OFFLINE_DATA_ROOT"]
    entities = {}
    batch_sources = {}
    push_sources = {}
    views = {}
    for view_name, spec in FEATURE_SPECS.items():
        entity = Entity(
            name=spec["entity_name"],
            join_keys=[spec["entity_key"]],
            value_type=ValueType.STRING,
        )
        batch_source = FileSource(
            name=f"{view_name}_source",
            path=join_data_root(root, spec["table"]),
            timestamp_field="event_timestamp",
        )
        push_source = PushSource(
            name=f"{view_name}_push",
            batch_source=batch_source,
        )
        integer_features = set(spec["integer_features"])
        view = FeatureView(
            name=view_name,
            entities=[entity],
            ttl=timedelta(days=30, minutes=10),
            schema=[
                Field(
                    name=feature,
                    dtype=Int64 if feature in integer_features else Float64,
                )
                for feature in spec["features"]
            ],
            source=push_source,
            online=True,
            enable_validation=True,
            tags={
                "layer": "online",
                "temporal_contract": "[T-window,T)",
                "contains_labels": "false",
                "owner": "streaming-block-a",
            },
        )
        entities[spec["entity_name"]] = entity
        batch_sources[view_name] = batch_source
        push_sources[view_name] = push_source
        views[view_name] = view
    return entities, batch_sources, push_sources, views


_entities, _batch_sources, _push_sources, _views = build_online_feature_objects()

user = _entities["user"]
account = _entities["account"]
device = _entities["device"]
merchant = _entities["merchant"]

user_behavior_push = _push_sources["user_behavior"]
account_behavior_push = _push_sources["account_behavior"]
device_behavior_push = _push_sources["device_behavior"]
merchant_behavior_push = _push_sources["merchant_behavior"]

user_behavior = _views["user_behavior"]
account_behavior = _views["account_behavior"]
device_behavior = _views["device_behavior"]
merchant_behavior = _views["merchant_behavior"]
