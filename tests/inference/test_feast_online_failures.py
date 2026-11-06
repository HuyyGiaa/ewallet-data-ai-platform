"""Feast adapter guards: construction, timestamps, timeouts and readiness."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import threading
import unittest
from unittest.mock import MagicMock

from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.inference.errors import (
    FeatureBackendError,
    FeatureContractError,
    FeatureTimeoutError,
    FeatureTimestampError,
    MissingFeatureError,
)
from ai_platform.inference.feast_online import (
    DEFAULT_MAX_STALENESS,
    FeastOnlineAdapter,
    _is_backend_timeout,
    _timestamp,
)
from ai_platform.inference.errors import StaleFeatureError
from ai_platform.inference.observability import NoopBackendTelemetry
from tests.inference.support import (
    FEATURE_MISSING,
    FEATURE_STALE,
    FEATURE_TIME,
    EntityFeatureStore,
    FeatureViewRef,
    OnlineResponse,
)
from tests.inference.test_assembler_boundaries import UnknownOffset
from tests.inference.test_feast_online import request


USER_COLUMNS = tuple(
    f"user_behavior__{name}" for name in FEATURE_SPECS["user_behavior"]["features"]
)
FIRST_COLUMN, SECOND_COLUMN = USER_COLUMNS[:2]


def adapter(store, **options) -> FeastOnlineAdapter:
    """Build an adapter that records no telemetry."""
    return FeastOnlineAdapter(
        store, backend_telemetry=NoopBackendTelemetry(), **options
    )


class StoreReturning(EntityFeatureStore):
    """Store whose online response is replaced by a transformation."""

    def __init__(self, transform):
        super().__init__()
        self._transform = transform

    def get_online_features(self, **kwargs):
        """Answer normally, then pass the data through the transformation."""
        response = super().get_online_features(**kwargs)
        replaced = self._transform(response.to_dict())
        return type(response)(replaced)


class ConstructionTest(unittest.TestCase):
    def test_non_positive_staleness_is_rejected(self):
        for value in (timedelta(0), timedelta(seconds=-1)):
            with self.assertRaises(ValueError):
                adapter(EntityFeatureStore(), max_staleness=value)

    def test_timeout_must_be_finite_and_bounded(self):
        for value in (0, -1, 60.5, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    adapter(EntityFeatureStore(), timeout_seconds=value)

    def test_timeout_upper_bound_is_inclusive(self):
        adapter(EntityFeatureStore(), timeout_seconds=60)


class TimestampParsingTest(unittest.TestCase):
    def test_numeric_and_aware_values_become_utc(self):
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        offset = timezone(timedelta(hours=7))
        self.assertEqual(epoch + timedelta(seconds=60), _timestamp(60))
        self.assertEqual(epoch + timedelta(seconds=1.5), _timestamp(1.5))
        converted = _timestamp(datetime(2026, 1, 1, 7, tzinfo=offset))
        self.assertEqual(datetime(2026, 1, 1, tzinfo=timezone.utc), converted)
        self.assertEqual(timedelta(0), converted.utcoffset())

    def test_unusable_values_are_rejected(self):
        for value in (
            True,
            "2026-01-01",
            float("nan"),
            float("inf"),
            10**20,
            datetime(2026, 1, 1),
            datetime(2026, 1, 1, tzinfo=UnknownOffset()),
        ):
            with self.subTest(value=value):
                with self.assertRaises(FeatureTimestampError):
                    _timestamp(value)


class BackendTimeoutDetectionTest(unittest.TestCase):
    def test_direct_and_chained_timeouts_are_found(self):
        wrapped = RuntimeError("outer")
        wrapped.__cause__ = TimeoutError("inner")
        contextual = RuntimeError("outer")
        contextual.__context__ = TimeoutError("inner")
        self.assertTrue(_is_backend_timeout(TimeoutError()))
        self.assertTrue(_is_backend_timeout(wrapped))
        self.assertTrue(_is_backend_timeout(contextual))

    def test_redis_timeout_is_recognized_by_origin(self):
        redis_timeout = type("TimeoutError", (Exception,), {"__module__": "redis.exceptions"})
        foreign_timeout = type("TimeoutError", (Exception,), {"__module__": "other"})
        self.assertTrue(_is_backend_timeout(redis_timeout()))
        self.assertFalse(_is_backend_timeout(foreign_timeout()))

    def test_exception_cycles_terminate(self):
        first, second = RuntimeError("a"), RuntimeError("b")
        first.__cause__, second.__cause__ = second, first
        self.assertFalse(_is_backend_timeout(first))


class FeatureRetrievalContractTest(unittest.TestCase):
    def test_non_mapping_response_violates_the_contract(self):
        store = StoreReturning(lambda data: ["not", "a", "dict"])
        with self.assertRaises(FeatureContractError):
            asyncio.run(adapter(store).get_features(request()))

    def test_entity_without_a_value_is_only_allowed_for_merchant(self):
        missing_entity = request().model_copy(update={"user_id": None})
        with self.assertRaises(FeatureContractError):
            asyncio.run(adapter(EntityFeatureStore()).get_features(missing_entity))

    def test_null_merchant_skips_the_merchant_view(self):
        store = EntityFeatureStore()
        store.get_online_features = MagicMock(wraps=store.get_online_features)
        result = asyncio.run(
            adapter(store).get_features(_recent(request(merchant_id=None)))
        )
        looked_up = [
            next(iter(call.kwargs["entity_rows"][0]))
            for call in store.get_online_features.call_args_list
        ]
        self.assertEqual(["user_id", "account_id", "device_id"], looked_up)
        self.assertIsNone(result.values["merchant_behavior__merchant_tx_count_1h"])
        self.assertIsNone(result.timestamps["merchant_behavior__merchant_tx_count_1h"])

    def test_scalar_feature_shape_is_reported_as_missing(self):
        name = "user_behavior__user_tx_count_1h"

        def flatten(data):
            if name in data:
                data[name] = 1
            return data

        with self.assertRaises(MissingFeatureError) as raised:
            asyncio.run(adapter(StoreReturning(flatten)).get_features(_recent(request())))
        self.assertEqual((name,), raised.exception.feature_names)

    def test_absent_timestamp_key_is_reported_as_missing(self):
        name = "user_behavior__user_tx_count_1h"

        def drop_timestamp(data):
            data.pop(f"{name}__ts", None)
            return data

        with self.assertRaises(MissingFeatureError):
            asyncio.run(
                adapter(StoreReturning(drop_timestamp)).get_features(_recent(request()))
            )

    def assert_missing(self, store, expected):
        with self.assertRaises(MissingFeatureError) as raised:
            asyncio.run(adapter(store).get_features(_recent(request())))
        self.assertEqual(tuple(expected), raised.exception.feature_names)

    def test_every_column_without_a_value_is_reported(self):
        def drop_values(data):
            data.pop(FIRST_COLUMN, None)
            data.pop(SECOND_COLUMN, None)
            return data

        self.assert_missing(StoreReturning(drop_values), (FIRST_COLUMN, SECOND_COLUMN))

    def test_every_column_without_a_timestamp_key_is_reported(self):
        def drop_timestamps(data):
            data.pop(f"{FIRST_COLUMN}__ts", None)
            data.pop(f"{SECOND_COLUMN}__ts", None)
            return data

        self.assert_missing(
            StoreReturning(drop_timestamps), (FIRST_COLUMN, SECOND_COLUMN)
        )

    def test_every_column_of_an_unknown_entity_is_reported(self):
        store = EntityFeatureStore(entity_faults={"user-1": FEATURE_MISSING})
        self.assert_missing(store, USER_COLUMNS)

    def test_every_required_null_value_is_reported(self):
        def clear_values(data):
            if FIRST_COLUMN in data:
                data[FIRST_COLUMN] = [None]
                data[SECOND_COLUMN] = [None]
            return data

        self.assert_missing(StoreReturning(clear_values), (FIRST_COLUMN, SECOND_COLUMN))

    def test_every_stale_column_is_reported(self):
        store = EntityFeatureStore(entity_faults={"user-1": FEATURE_STALE})
        with self.assertRaises(StaleFeatureError) as raised:
            asyncio.run(adapter(store).get_features(_recent(request())))
        self.assertEqual(USER_COLUMNS, raised.exception.feature_names)

    def test_staleness_boundary_is_inclusive(self):
        at_limit = _at(FEATURE_TIME + DEFAULT_MAX_STALENESS)
        result = asyncio.run(adapter(EntityFeatureStore()).get_features(at_limit))
        self.assertEqual(31, len(result.values))

    def test_feature_written_at_the_event_instant_is_not_future(self):
        result = asyncio.run(
            adapter(EntityFeatureStore()).get_features(_at(FEATURE_TIME))
        )
        self.assertEqual(31, len(result.values))

    def test_freshness_records_the_oldest_observed_age(self):
        telemetry = MagicMock(wraps=NoopBackendTelemetry())
        store = EntityFeatureStore()
        asyncio.run(
            FeastOnlineAdapter(store, backend_telemetry=telemetry).get_features(
                _at(FEATURE_TIME + timedelta(seconds=30))
            )
        )
        telemetry.record_feast_freshness.assert_called_once_with(30.0)


class BoundedExecutionTest(unittest.TestCase):
    def test_hung_store_call_times_out(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class Hanging(EntityFeatureStore):
            def get_online_features(self, **kwargs):
                release.wait(0.2)
                return super().get_online_features(**kwargs)

        with self.assertRaises(FeatureTimeoutError):
            asyncio.run(
                adapter(Hanging(), timeout_seconds=0.01).get_features(request())
            )


class ReadinessTest(unittest.TestCase):
    def test_probe_uses_sentinel_entities_for_every_view(self):
        store = EntityFeatureStore()
        store.get_online_features = MagicMock(wraps=store.get_online_features)
        self.assertTrue(asyncio.run(adapter(store).ready()))
        self.assertEqual(len(FEATURE_SPECS), store.get_online_features.call_count)
        for call in store.get_online_features.call_args_list:
            self.assertEqual(
                {"__block_b_readiness__"},
                set(call.kwargs["entity_rows"][0].values()),
            )

    def test_probe_requests_the_same_response_shape_as_serving(self):
        class RecordingResponse(OnlineResponse):
            requested = []

            def to_dict(self, include_event_timestamps=False):
                """Record the flag that serving depends on."""
                self.requested.append(include_event_timestamps)
                return super().to_dict(include_event_timestamps)

        store = EntityFeatureStore()
        answer = store.get_online_features

        def recording(**kwargs):
            """Wrap the store's answer in a recording response."""
            return RecordingResponse(answer(**kwargs).to_dict())

        store.get_online_features = MagicMock(side_effect=recording)
        asyncio.run(adapter(store).ready())
        for call in store.get_online_features.call_args_list:
            self.assertIs(True, call.kwargs["full_feature_names"])
        self.assertEqual(
            [True] * len(FEATURE_SPECS), RecordingResponse.requested
        )

    def test_registry_failures_are_classified(self):
        for error, expected in (
            (ConnectionError("down"), FeatureBackendError),
            (TimeoutError("slow"), FeatureTimeoutError),
        ):
            with self.subTest(error=type(error).__name__):
                store = EntityFeatureStore()
                store.list_feature_views = MagicMock(side_effect=error)
                with self.assertRaises(expected):
                    asyncio.run(adapter(store).ready())

    def test_registry_must_expose_exactly_the_contract_views(self):
        store = EntityFeatureStore()
        store.list_feature_views = MagicMock(
            return_value=[FeatureViewRef("user_behavior")]
        )
        with self.assertRaises(FeatureContractError):
            asyncio.run(adapter(store).ready())

    def test_probe_failures_are_classified(self):
        for error, expected in (
            (ConnectionError("down"), FeatureBackendError),
            (TimeoutError("slow"), FeatureTimeoutError),
        ):
            with self.subTest(error=type(error).__name__):
                store = EntityFeatureStore()
                store.get_online_features = MagicMock(side_effect=error)
                with self.assertRaises(expected):
                    asyncio.run(adapter(store).ready())

    def test_probe_response_must_be_a_mapping(self):
        store = StoreReturning(lambda data: "not-a-mapping")
        with self.assertRaises(FeatureContractError):
            asyncio.run(adapter(store).ready())


def _at(event_time: datetime):
    return request().model_copy(update={"event_timestamp": event_time})


def _recent(model_request):
    return model_request.model_copy(
        update={"event_timestamp": FEATURE_TIME + timedelta(minutes=1)}
    )


if __name__ == "__main__":
    unittest.main()
