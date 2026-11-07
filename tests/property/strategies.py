"""Reusable Hypothesis generators for Fraud API and agent contracts.

This module only describes input spaces. What must hold for those inputs lives
in ``assertions.py`` and the test modules. All generated data is synthetic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import string
from typing import Callable

from hypothesis import strategies as st

from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.inference.assembler import HistoricalFeatureSet
from ai_platform.inference.feast_online import DEFAULT_MAX_STALENESS
from ai_platform.inference.schemas import (
    FraudPredictionRequest,
    TransactionChannel,
    TransactionType,
)
from ai_platform.ml.training.contract import (
    HISTORICAL_FEATURE_COLUMNS,
    NULLABLE_HISTORICAL_FEATURE_COLUMNS,
)
from tests.inference.support import FEATURE_TIME


MERCHANT_COLUMNS = tuple(
    name for name in HISTORICAL_FEATURE_COLUMNS if name.startswith("merchant_behavior__")
)
INTEGER_COLUMNS = frozenset(
    f"{view_name}__{feature}"
    for view_name, spec in FEATURE_SPECS.items()
    for feature in spec["integer_features"]
)
MAX_ID_LENGTH = 128
_ID_ALPHABET = string.ascii_letters + string.digits + "-_.:"
_MAX_FRESH_SECONDS = int(DEFAULT_MAX_STALENESS.total_seconds())

canonical_ids = st.text(_ID_ALPHABET, min_size=1, max_size=MAX_ID_LENGTH)
short_ids = st.text(_ID_ALPHABET, min_size=1, max_size=24)
positive_amounts = st.floats(
    min_value=0.0, exclude_min=True, allow_nan=False, allow_infinity=False
)
utc_offsets = st.integers(min_value=-14 * 60, max_value=14 * 60).map(
    lambda minutes: timezone(timedelta(minutes=minutes))
)
blank_text = st.text(" \t\r\n", max_size=8)


@st.composite
def fresh_event_times(draw: st.DrawFn) -> datetime:
    """An aware instant the shared feature rows are still fresh for."""
    age = timedelta(seconds=draw(st.integers(0, _MAX_FRESH_SECONDS)))
    return (FEATURE_TIME + age).astimezone(draw(utc_offsets))


@st.composite
def valid_payloads(draw: st.DrawFn, ids: st.SearchStrategy[str] = canonical_ids) -> dict:
    """JSON-ready requests honouring the conditional entity rules."""
    transaction_type = draw(st.sampled_from(TransactionType))
    optional_id = st.none() | ids
    merchant = ids if transaction_type is TransactionType.PAYMENT else optional_id
    counterparty = ids if transaction_type is TransactionType.TRANSFER else optional_id
    return {
        "transaction_id": draw(ids),
        "event_timestamp": draw(fresh_event_times()).isoformat(),
        "user_id": draw(ids),
        "account_id": draw(ids),
        "device_id": draw(ids),
        "merchant_id": draw(merchant),
        "counterparty_account_id": draw(counterparty),
        "type": transaction_type.value,
        "amount": draw(positive_amounts),
        "currency": "VND",
        "channel": draw(st.sampled_from(TransactionChannel)).value,
    }


def valid_requests() -> st.SearchStrategy[FraudPredictionRequest]:
    """Validated request models for tests below the HTTP boundary."""
    return valid_payloads().map(FraudPredictionRequest.model_validate)


def _with(field: str, values: st.SearchStrategy[object]) -> st.SearchStrategy[dict]:
    return st.builds(
        lambda payload, value: {**payload, field: value}, valid_payloads(), values
    )


def _without(fields: tuple[str, ...]) -> st.SearchStrategy[dict]:
    return st.builds(
        lambda payload, field: {k: v for k, v in payload.items() if k != field},
        valid_payloads(),
        st.sampled_from(fields),
    )


def _conditional_entity_missing(
    transaction_type: str, field: str
) -> st.SearchStrategy[dict]:
    return valid_payloads().map(
        lambda payload: {**payload, "type": transaction_type, field: None}
    )


REQUIRED_FIELDS = (
    "transaction_id",
    "event_timestamp",
    "user_id",
    "account_id",
    "device_id",
    "type",
    "amount",
    "currency",
    "channel",
)
ID_FIELDS = ("transaction_id", "user_id", "account_id", "device_id")

_non_positive = st.floats(max_value=0.0, allow_nan=False, allow_infinity=False)
_oversized_ids = st.text(
    _ID_ALPHABET, min_size=MAX_ID_LENGTH + 1, max_size=MAX_ID_LENGTH + 32
)
_naive_timestamps = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1)
).map(datetime.isoformat)


def invalid_payload_classes() -> dict[str, st.SearchStrategy[dict]]:
    """Each named partition of the request space that must be rejected."""
    classes: dict[str, st.SearchStrategy[dict]] = {
        "non_positive_amount": _with("amount", _non_positive),
        "non_finite_amount": _with(
            "amount", st.sampled_from([float("nan"), float("inf"), float("-inf")])
        ),
        "unsupported_currency": _with(
            "currency", st.text(max_size=8).filter(lambda value: value != "VND")
        ),
        "unknown_type": _with(
            "type",
            st.text(max_size=12).filter(
                lambda value: value not in {item.value for item in TransactionType}
            ),
        ),
        "unknown_channel": _with(
            "channel",
            st.text(max_size=12).filter(
                lambda value: value not in {item.value for item in TransactionChannel}
            ),
        ),
        "naive_timestamp": _with("event_timestamp", _naive_timestamps),
        "unknown_field": st.builds(
            lambda payload, name: {**payload, f"extra_{name}": 1},
            valid_payloads(),
            short_ids,
        ),
        "missing_required_field": _without(REQUIRED_FIELDS),
        "payment_without_merchant": _conditional_entity_missing("payment", "merchant_id"),
        "transfer_without_counterparty": _conditional_entity_missing(
            "transfer", "counterparty_account_id"
        ),
    }
    for field in ID_FIELDS:
        classes[f"blank_{field}"] = _with(field, blank_text)
        classes[f"oversized_{field}"] = _with(field, _oversized_ids)
    return classes


def invalid_payloads() -> st.SearchStrategy[dict]:
    """Any payload from any rejected partition."""
    return st.one_of(*invalid_payload_classes().values())


def _feature_value(column: str) -> st.SearchStrategy[object]:
    if column in INTEGER_COLUMNS:
        values: st.SearchStrategy[object] = st.integers(0, 2**31)
    else:
        values = st.floats(
            min_value=-1e12, max_value=1e12, allow_nan=False, allow_infinity=False
        )
    if column in NULLABLE_HISTORICAL_FEATURE_COLUMNS:
        values = st.none() | values
    return values


@st.composite
def historical_features(
    draw: st.DrawFn, request: FraudPredictionRequest
) -> HistoricalFeatureSet:
    """A contract-complete feature set for ``request``.

    Merchant columns are null when the request has no merchant, as the adapter
    returns them. Column insertion order is deliberately randomized.
    """
    columns = draw(st.permutations(HISTORICAL_FEATURE_COLUMNS))
    values: dict[str, object] = {}
    timestamps: dict[str, datetime | None] = {}
    for column in columns:
        if request.merchant_id is None and column in MERCHANT_COLUMNS:
            values[column] = None
            timestamps[column] = None
            continue
        values[column] = draw(_feature_value(column))
        age = timedelta(seconds=draw(st.integers(0, _MAX_FRESH_SECONDS)))
        timestamps[column] = FEATURE_TIME - age
    return HistoricalFeatureSet(values=values, timestamps=timestamps)


@st.composite
def requests_with_features(
    draw: st.DrawFn,
) -> tuple[FraudPredictionRequest, HistoricalFeatureSet]:
    """A request paired with features that satisfy its contract."""
    request = draw(valid_requests())
    return request, draw(historical_features(request))


def distinct_pairs(values: st.SearchStrategy, key: Callable = lambda item: item):
    """Two values that differ under ``key``."""
    return st.tuples(values, values).filter(lambda pair: key(pair[0]) != key(pair[1]))


sha256_fingerprints = st.text("0123456789abcdef", min_size=64, max_size=64)
scope_parts = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", fullmatch=True)
request_ids = st.text(string.ascii_letters + string.digits + "-", min_size=1, max_size=64)
result_documents = st.dictionaries(
    st.text(string.ascii_lowercase, min_size=1, max_size=6),
    st.integers() | st.text(max_size=12) | st.booleans(),
    max_size=4,
)
duplicate_counts = st.integers(min_value=1, max_value=6)
