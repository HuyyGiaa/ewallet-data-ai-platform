"""Request-scoped Feast Online retrieval with frozen freshness enforcement."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import math
from typing import Protocol

from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.inference.assembler import HistoricalFeatureSet
from ai_platform.inference.errors import (
    FeatureBackendError,
    FeatureContractError,
    FeatureTimestampError,
    FeatureTimeoutError,
    MissingFeatureError,
    StaleFeatureError,
)
from ai_platform.inference.schemas import FraudPredictionRequest
from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS


DEFAULT_MAX_STALENESS = timedelta(minutes=10)
_TIMESTAMP_SUFFIX = "__ts"
_MERCHANT_FEATURES = frozenset(
    name for name in HISTORICAL_FEATURE_COLUMNS if name.startswith("merchant_behavior__")
)
_READINESS_ENTITY_VALUE = "__block_b_readiness__"
DEFAULT_TIMEOUT_SECONDS = 2.0


class OnlineFeatureProvider(Protocol):
    async def get_features(
        self, request: FraudPredictionRequest
    ) -> HistoricalFeatureSet: ...

    async def ready(self) -> bool: ...


def _one(data: dict[str, object], name: str) -> object:
    values = data.get(name)
    if not isinstance(values, (list, tuple)) or len(values) != 1:
        raise MissingFeatureError([name.removesuffix(_TIMESTAMP_SUFFIX)])
    return values[0]


def _timestamp(value: object) -> datetime:
    if isinstance(value, bool):
        raise FeatureTimestampError()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise FeatureTimestampError() from exc
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise FeatureTimestampError()
        return value.astimezone(timezone.utc)
    raise FeatureTimestampError()


def _is_backend_timeout(exc: BaseException) -> bool:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, TimeoutError):
            return True
        if (
            current.__class__.__name__ == "TimeoutError"
            and current.__class__.__module__.startswith("redis")
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


class FeastOnlineAdapter:
    """Read the canonical 31 features without Redis-specific key access."""

    def __init__(
        self,
        store: object,
        *,
        max_staleness: timedelta = DEFAULT_MAX_STALENESS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if max_staleness <= timedelta(0):
            raise ValueError("max_staleness must be positive")
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > 60
        ):
            raise ValueError("timeout_seconds must be positive and at most 60")
        self._store = store
        self._max_staleness = max_staleness
        self._timeout_seconds = float(timeout_seconds)

    async def _run_bounded(self, function, *args):
        try:
            # Cancelling wait_for does not terminate a worker already running in
            # asyncio's thread pool. Redis connect/read socket deadlines are
            # configured separately at application construction time.
            return await asyncio.wait_for(
                asyncio.to_thread(function, *args),
                timeout=self._timeout_seconds,
            )
        except FeatureTimeoutError:
            raise
        except asyncio.TimeoutError as exc:
            raise FeatureTimeoutError() from exc

    async def get_features(
        self, request: FraudPredictionRequest
    ) -> HistoricalFeatureSet:
        return await self._run_bounded(self._get_features, request)

    def _get_features(
        self, request: FraudPredictionRequest
    ) -> HistoricalFeatureSet:
        entity_values = {
            "user_id": request.user_id,
            "account_id": request.account_id,
            "device_id": request.device_id,
            "merchant_id": request.merchant_id,
        }
        data: dict[str, object] = {}
        for view_name, spec in FEATURE_SPECS.items():
            entity_key = spec["entity_key"]
            entity_value = entity_values[entity_key]
            if entity_value is None:
                if view_name != "merchant_behavior":
                    raise FeatureContractError()
                for feature_name in spec["features"]:
                    full_name = f"{view_name}__{feature_name}"
                    data[full_name] = [None]
                    data[full_name + _TIMESTAMP_SUFFIX] = [None]
                continue

            feature_refs = [
                f"{view_name}:{feature_name}"
                for feature_name in spec["features"]
            ]
            try:
                response = self._store.get_online_features(
                    features=feature_refs,
                    entity_rows=[{entity_key: entity_value}],
                    full_feature_names=True,
                )
                view_data = response.to_dict(include_event_timestamps=True)
            except Exception as exc:
                if _is_backend_timeout(exc):
                    raise FeatureTimeoutError() from exc
                raise FeatureBackendError() from exc
            if not isinstance(view_data, dict):
                raise FeatureContractError()

            expected_view_features = {
                feature_ref.replace(":", "__", 1)
                for feature_ref in feature_refs
            }
            allowed = {entity_key, *expected_view_features}
            allowed.update(
                name + _TIMESTAMP_SUFFIX for name in expected_view_features
            )
            if set(view_data).difference(allowed):
                raise FeatureContractError()
            data.update(view_data)

        values: dict[str, object] = {}
        timestamps: dict[str, datetime | None] = {}
        missing: list[str] = []
        stale: list[str] = []
        future: list[str] = []
        request_time = request.event_timestamp.astimezone(timezone.utc)
        for name in HISTORICAL_FEATURE_COLUMNS:
            not_applicable = request.merchant_id is None and name in _MERCHANT_FEATURES
            try:
                value = _one(data, name)
            except MissingFeatureError:
                missing.append(name)
                continue
            if not_applicable:
                if value is not None:
                    raise FeatureContractError()
                values[name] = None
                timestamps[name] = None
                continue
            if value is None:
                missing.append(name)
                continue
            try:
                feature_time = _timestamp(_one(data, name + _TIMESTAMP_SUFFIX))
            except MissingFeatureError:
                missing.append(name)
                continue
            age = request_time - feature_time
            if age < timedelta(0):
                future.append(name)
            elif age > self._max_staleness:
                stale.append(name)
            values[name] = value
            timestamps[name] = feature_time

        if missing:
            raise MissingFeatureError(missing)
        if future:
            raise FeatureTimestampError()
        if stale:
            raise StaleFeatureError(stale)
        return HistoricalFeatureSet(values=values, timestamps=timestamps)

    async def ready(self) -> bool:
        """Check schema and backend connectivity only.

        The sentinel entities are not evidence of feature materialization,
        freshness, correctness, or offline/online parity.
        """
        return await self._run_bounded(self._connectivity_ready)

    def _connectivity_ready(self) -> bool:
        try:
            views = self._store.list_feature_views()
        except Exception as exc:
            if _is_backend_timeout(exc):
                raise FeatureTimeoutError() from exc
            raise FeatureBackendError() from exc
        names = {view.name for view in views}
        if names != set(FEATURE_SPECS):
            raise FeatureContractError()

        def probe_online_store() -> None:
            for view_name, spec in FEATURE_SPECS.items():
                feature_refs = [
                    f"{view_name}:{feature_name}"
                    for feature_name in spec["features"]
                ]
                response = self._store.get_online_features(
                    features=feature_refs,
                    entity_rows=[
                        {spec["entity_key"]: _READINESS_ENTITY_VALUE}
                    ],
                    full_feature_names=True,
                )
                data = response.to_dict(include_event_timestamps=True)
                if not isinstance(data, dict):
                    raise FeatureContractError()

        try:
            probe_online_store()
        except FeatureContractError:
            raise
        except Exception as exc:
            if _is_backend_timeout(exc):
                raise FeatureTimeoutError() from exc
            raise FeatureBackendError() from exc
        return True
