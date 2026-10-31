"""Deterministic online assembly of the frozen 41-feature model vector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from numbers import Integral, Real
from types import MappingProxyType
from typing import Mapping

import numpy as np
import pandas as pd

from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.inference.errors import (
    FeatureContractError,
    MissingFeatureError,
)
from ai_platform.inference.schemas import FraudPredictionRequest
from ai_platform.ml.training.contract import (
    HISTORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
)
from ai_platform.ml.training.derived import add_derived_features


_EXPECTED_HISTORICAL = frozenset(HISTORICAL_FEATURE_COLUMNS)
_MERCHANT_FEATURES = frozenset(
    column
    for column in HISTORICAL_FEATURE_COLUMNS
    if column.startswith("merchant_behavior__")
)
_INTEGER_FEATURES = frozenset(
    f"{view_name}__{feature}"
    for view_name, spec in FEATURE_SPECS.items()
    for feature in spec["integer_features"]
)


@dataclass(frozen=True)
class HistoricalFeatureSet:
    values: Mapping[str, object]
    timestamps: Mapping[str, datetime | None]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(
            self, "timestamps", MappingProxyType(dict(self.timestamps))
        )


@dataclass(frozen=True)
class ModelInputVector:
    columns: tuple[str, ...]
    values: tuple[object, ...]
    feature_timestamp: datetime

    def __post_init__(self) -> None:
        if self.columns != MODEL_FEATURE_COLUMNS or len(self.values) != 41:
            raise FeatureContractError()

    def as_dict(self) -> dict[str, object]:
        return dict(zip(self.columns, self.values, strict=True))


def _validate_historical_type(column: str, value: object) -> object:
    if isinstance(value, bool):
        raise FeatureContractError()
    if column in _INTEGER_FEATURES:
        if not isinstance(value, Integral):
            raise FeatureContractError()
        return int(value)
    if not isinstance(value, Real) or not math.isfinite(float(value)):
        raise FeatureContractError()
    return float(value)


def _json_compatible(value: object) -> object:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        return numeric
    return value


class CanonicalFeatureAssembler:
    """Reuse training formulas while enforcing online completeness and order."""

    def assemble(
        self,
        request: FraudPredictionRequest,
        historical: HistoricalFeatureSet,
    ) -> ModelInputVector:
        value_names = set(historical.values)
        timestamp_names = set(historical.timestamps)
        if value_names != _EXPECTED_HISTORICAL:
            raise FeatureContractError()
        if timestamp_names != _EXPECTED_HISTORICAL:
            raise FeatureContractError()

        missing: list[str] = []
        normalized: dict[str, object] = {}
        contributing_timestamps: list[datetime] = []
        for column in HISTORICAL_FEATURE_COLUMNS:
            value = historical.values[column]
            timestamp = historical.timestamps[column]
            not_applicable = request.merchant_id is None and column in _MERCHANT_FEATURES
            if not_applicable:
                if value is not None or timestamp is not None:
                    raise FeatureContractError()
                normalized[column] = None
                continue
            if value is None or timestamp is None:
                missing.append(column)
                continue
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise FeatureContractError()
            normalized[column] = _validate_historical_type(column, value)
            contributing_timestamps.append(timestamp)

        if missing:
            raise MissingFeatureError(missing)
        if not contributing_timestamps:
            raise MissingFeatureError(list(HISTORICAL_FEATURE_COLUMNS))

        row = {**request.request_time_features(), **normalized}
        derived = add_derived_features(pd.DataFrame([row])).iloc[0]
        values = tuple(
            _json_compatible(derived[column]) for column in MODEL_FEATURE_COLUMNS
        )
        return ModelInputVector(
            columns=MODEL_FEATURE_COLUMNS,
            values=values,
            feature_timestamp=min(contributing_timestamps),
        )
