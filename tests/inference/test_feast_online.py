from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import time
import unittest

from feast.online_response import OnlineResponse
from feast.protos.feast.serving.ServingService_pb2 import (
    GetOnlineFeaturesResponse,
)

from ai_platform.features.feast.schema import ALL_FEATURE_REFS, FEATURE_SPECS
from ai_platform.inference.errors import (
    FeatureBackendError,
    FeatureContractError,
    FeatureTimeoutError,
    FeatureTimestampError,
    MissingFeatureError,
    StaleFeatureError,
)
from ai_platform.inference.feast_online import (
    DEFAULT_MAX_STALENESS,
    FeastOnlineAdapter,
)
from ai_platform.inference.schemas import FraudPredictionRequest
from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS


UTC = timezone.utc
EVENT_TIME = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def request(merchant_id: str | None = "merchant-1") -> FraudPredictionRequest:
    return FraudPredictionRequest.model_validate(
        {
            "transaction_id": "tx-1",
            "event_timestamp": EVENT_TIME,
            "user_id": "user-1",
            "account_id": "account-1",
            "device_id": "device-1",
            "merchant_id": merchant_id,
            "counterparty_account_id": None,
            "type": "payment" if merchant_id else "withdraw",
            "amount": 120.0,
            "currency": "VND",
            "channel": "app",
        }
    )


def response_data(timestamp: datetime = EVENT_TIME - timedelta(minutes=2)):
    result: dict[str, list[object]] = {}
    integer_names = {
        f"{view_name}__{name}"
        for view_name, spec in FEATURE_SPECS.items()
        for name in spec["integer_features"]
    }
    for index, name in enumerate(HISTORICAL_FEATURE_COLUMNS):
        result[name] = [index + 1 if name in integer_names else float(index + 1)]
        result[f"{name}__ts"] = [int(timestamp.timestamp())]
    result.update(
        {
            "user_id": ["user-1"],
            "account_id": ["account-1"],
            "device_id": ["device-1"],
            "merchant_id": ["merchant-1"],
        }
    )
    return result


class FakeResponse:
    def __init__(self, data):
        self.data = data
        self.include_event_timestamps = None

    def to_dict(self, include_event_timestamps=False):
        self.include_event_timestamps = include_event_timestamps
        return self.data


class FakeView:
    def __init__(self, name):
        self.name = name


class FakeStore:
    def __init__(self, data=None, error: Exception | None = None, return_all=False):
        self.data = data or response_data()
        self.responses = []
        self.error = error
        self.return_all = return_all
        self.calls = []
        self.views = [FakeView(name) for name in FEATURE_SPECS]

    def get_online_features(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if self.return_all:
            selected = self.data
        else:
            selected = {}
            for feature_ref in kwargs["features"]:
                name = feature_ref.replace(":", "__", 1)
                for key in (name, name + "__ts"):
                    if key in self.data:
                        selected[key] = self.data[key]
            for entity_row in kwargs["entity_rows"]:
                for key in entity_row:
                    if key in self.data:
                        selected[key] = self.data[key]
        response = FakeResponse(selected)
        self.responses.append(response)
        return response

    def list_feature_views(self):
        if self.error:
            raise self.error
        return self.views


class FeastOnlineAdapterTest(unittest.TestCase):
    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def test_retrieves_all_31_features_with_canonical_entity_mapping(self) -> None:
        store = FakeStore()
        result = self.run_async(FeastOnlineAdapter(store).get_features(request()))
        self.assertEqual(set(HISTORICAL_FEATURE_COLUMNS), set(result.values))
        self.assertEqual(31, len(result.values))
        self.assertEqual(
            [
                {"user_id": "user-1"},
                {"account_id": "account-1"},
                {"device_id": "device-1"},
                {"merchant_id": "merchant-1"},
            ],
            [call["entity_rows"][0] for call in store.calls],
        )
        self.assertEqual(
            list(ALL_FEATURE_REFS),
            [feature for call in store.calls for feature in call["features"]],
        )
        self.assertTrue(
            all(call["full_feature_names"] for call in store.calls)
        )
        self.assertTrue(
            all(response.include_event_timestamps for response in store.responses)
        )

    def test_pinned_feast_066_to_dict_timestamp_and_missing_value_shape(self) -> None:
        proto = GetOnlineFeaturesResponse()
        proto.metadata.feature_names.val.extend(
            ["user_behavior__user_tx_count_5m", "user_behavior__missing"]
        )
        populated = proto.results.add()
        populated.values.add().int64_val = 3
        populated.event_timestamps.add(seconds=int(EVENT_TIME.timestamp()))
        missing = proto.results.add()
        missing.values.add()
        missing.event_timestamps.add(seconds=0)

        self.assertEqual(
            {
                "user_behavior__user_tx_count_5m": [3],
                "user_behavior__user_tx_count_5m__ts": [
                    int(EVENT_TIME.timestamp())
                ],
                "user_behavior__missing": [None],
                "user_behavior__missing__ts": [0],
            },
            OnlineResponse(proto).to_dict(include_event_timestamps=True),
        )

    def test_exactly_ten_minutes_old_is_not_stale(self) -> None:
        store = FakeStore(response_data(EVENT_TIME - timedelta(minutes=10)))
        result = self.run_async(FeastOnlineAdapter(store).get_features(request()))
        self.assertEqual(31, len(result.values))
        self.assertEqual(timedelta(minutes=10), DEFAULT_MAX_STALENESS)

    def test_missing_feature_value_and_timestamp_are_distinct_missing_failures(self) -> None:
        cases = []
        missing_key = response_data()
        missing_key.pop("user_behavior__user_tx_count_5m")
        cases.append(missing_key)

        missing_value = response_data()
        missing_value["account_behavior__account_tx_count_1h"] = [None]
        cases.append(missing_value)

        missing_timestamp = response_data()
        missing_timestamp.pop("device_behavior__device_tx_count_1h__ts")
        cases.append(missing_timestamp)

        for data in cases:
            with self.subTest(keys=len(data)):
                with self.assertRaises(MissingFeatureError):
                    self.run_async(
                        FeastOnlineAdapter(FakeStore(data)).get_features(request())
                    )

    def test_stale_features_are_not_reported_as_backend_failure(self) -> None:
        store = FakeStore(response_data(EVENT_TIME - timedelta(minutes=11)))
        with self.assertRaises(StaleFeatureError):
            self.run_async(FeastOnlineAdapter(store).get_features(request()))

    def test_future_feature_timestamp_fails_contract(self) -> None:
        store = FakeStore(response_data(EVENT_TIME + timedelta(seconds=1)))
        with self.assertRaises(FeatureTimestampError):
            self.run_async(FeastOnlineAdapter(store).get_features(request()))

    def test_non_finite_feature_timestamp_is_classified(self) -> None:
        data = response_data()
        data["user_behavior__user_tx_count_5m__ts"] = [float("nan")]
        with self.assertRaises(FeatureTimestampError):
            self.run_async(FeastOnlineAdapter(FakeStore(data)).get_features(request()))

    def test_backend_error_is_classified_without_leaking_backend_message(self) -> None:
        store = FakeStore(error=RuntimeError("redis password=secret"))
        with self.assertRaises(FeatureBackendError) as captured:
            self.run_async(FeastOnlineAdapter(store).get_features(request()))
        self.assertNotIn("secret", str(captured.exception))

    def test_backend_timeout_is_distinct_from_generic_backend_failure(self) -> None:
        store = FakeStore(error=TimeoutError("redis read timed out"))
        with self.assertRaises(FeatureTimeoutError):
            self.run_async(FeastOnlineAdapter(store).get_features(request()))

    def test_feature_retrieval_deadline_does_not_claim_worker_cancellation(self) -> None:
        class SlowStore(FakeStore):
            def __init__(self):
                super().__init__()
                self.first_call_finished = False

            def get_online_features(self, **kwargs):
                time.sleep(0.05)
                self.first_call_finished = True
                return super().get_online_features(**kwargs)

        store = SlowStore()

        async def scenario():
            with self.assertRaises(FeatureTimeoutError):
                await FeastOnlineAdapter(
                    store, timeout_seconds=0.001
                ).get_features(request())
            self.assertFalse(store.first_call_finished)

        self.run_async(scenario())
        self.assertTrue(store.first_call_finished)

    def test_readiness_deadline_is_classified(self) -> None:
        class SlowStore(FakeStore):
            def list_feature_views(self):
                time.sleep(0.05)
                return super().list_feature_views()

        with self.assertRaises(FeatureTimeoutError):
            self.run_async(
                FeastOnlineAdapter(
                    SlowStore(), timeout_seconds=0.001
                ).ready()
            )

    def test_null_merchant_allows_only_explicit_null_merchant_features(self) -> None:
        data = response_data()
        data["merchant_id"] = [None]
        for name in HISTORICAL_FEATURE_COLUMNS:
            if name.startswith("merchant_behavior__"):
                data[name] = [None]
                data[name + "__ts"] = [0]
        store = FakeStore(data)
        result = self.run_async(
            FeastOnlineAdapter(store).get_features(request(merchant_id=None))
        )
        for name in HISTORICAL_FEATURE_COLUMNS:
            if name.startswith("merchant_behavior__"):
                self.assertIsNone(result.values[name])
                self.assertIsNone(result.timestamps[name])
        self.assertEqual(3, len(store.calls))
        self.assertNotIn(
            "merchant_id",
            {key for call in store.calls for key in call["entity_rows"][0]},
        )

    def test_unexpected_online_feature_is_contract_drift(self) -> None:
        data = response_data()
        data["user_behavior__new_feature"] = [1]
        data["user_behavior__new_feature__ts"] = [int(EVENT_TIME.timestamp())]
        with self.assertRaises(FeatureContractError):
            self.run_async(
                FeastOnlineAdapter(FakeStore(data, return_all=True)).get_features(request())
            )

    def test_readiness_requires_all_four_canonical_views(self) -> None:
        store = FakeStore()
        adapter = FeastOnlineAdapter(store)
        self.assertTrue(self.run_async(adapter.ready()))
        self.assertEqual(4, len(store.calls))
        self.assertTrue(
            all(
                list(call["entity_rows"][0].values())
                == ["__block_b_readiness__"]
                for call in store.calls
            )
        )
        store.views.pop()
        with self.assertRaises(FeatureContractError):
            self.run_async(adapter.ready())

    def test_readiness_probe_is_connectivity_only_not_materialization_proof(self) -> None:
        class ConnectivityOnlyStore(FakeStore):
            def get_online_features(self, **kwargs):
                self.calls.append(kwargs)
                return FakeResponse({})

        store = ConnectivityOnlyStore()
        self.assertTrue(self.run_async(FeastOnlineAdapter(store).ready()))
        self.assertEqual(4, len(store.calls))


if __name__ == "__main__":
    unittest.main()
