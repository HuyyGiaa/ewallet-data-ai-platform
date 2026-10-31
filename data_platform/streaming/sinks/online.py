"""Feast-compatible online feature update envelope."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json

from ai_platform.features.feast.schema import FEATURE_SPECS


@dataclass(frozen=True)
class OnlineFeatureUpdate:
    view_name: str
    entity_name: str
    entity_key: str
    entity_value: str
    feature_timestamp: datetime
    values: dict[str, float | int | None]
    source_event_id: str

    def __post_init__(self) -> None:
        spec = FEATURE_SPECS.get(self.view_name)
        if not spec:
            raise ValueError(f"unknown FeatureView: {self.view_name}")
        if self.entity_name != spec["entity_name"] or self.entity_key != spec["entity_key"]:
            raise ValueError("entity mapping differs from canonical Feast schema")
        expected = set(spec["features"])
        actual = set(self.values)
        if actual != expected:
            raise ValueError(
                f"complete FeatureView row required for {self.view_name}; "
                f"missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}"
            )

    def feast_row(self) -> dict[str, object]:
        return {
            self.entity_key: self.entity_value,
            "event_timestamp": self.feature_timestamp,
            **self.values,
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "view_name": self.view_name,
                "entity_name": self.entity_name,
                "entity_key": self.entity_key,
                "entity_value": self.entity_value,
                "feature_timestamp": self.feature_timestamp.isoformat(),
                "values": self.values,
                "source_event_id": self.source_event_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> "OnlineFeatureUpdate":
        values = json.loads(payload)
        values["feature_timestamp"] = datetime.fromisoformat(values["feature_timestamp"])
        return cls(**values)

    @property
    def message_key(self) -> str:
        return f"{self.entity_name}:{self.entity_value}"
