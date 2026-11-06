"""Invariants of the 41-input feature vector and its V2 wire encoding."""

from __future__ import annotations

import random

from hypothesis import given, strategies as st
import pytest

from ai_platform.inference.assembler import (
    CanonicalFeatureAssembler,
    HistoricalFeatureSet,
)
from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS
from ai_platform.inference.errors import FeatureContractError, MissingFeatureError
from ai_platform.inference.wire import FraudV2WireContract
from ai_platform.ml.training.contract import (
    HISTORICAL_FEATURE_COLUMNS,
    NULLABLE_HISTORICAL_FEATURE_COLUMNS,
)
from tests.inference.support import model_metadata, v2_input_datatype
from tests.property.assertions import assert_vector_contract
from tests.property.strategies import MERCHANT_COLUMNS, requests_with_features


ASSEMBLER = CanonicalFeatureAssembler()
REQUIRED_COLUMNS = tuple(
    column
    for column in HISTORICAL_FEATURE_COLUMNS
    if column not in NULLABLE_HISTORICAL_FEATURE_COLUMNS
    and column not in MERCHANT_COLUMNS
)


def _contract() -> FraudV2WireContract:
    contract = FraudV2WireContract()
    contract.validate_metadata(model_metadata())
    return contract


@given(requests_with_features())
def test_vector_keeps_the_frozen_order_values_and_formulas(case):
    request, features = case
    assert_vector_contract(ASSEMBLER.assemble(request, features), request, features)


@given(requests_with_features(), st.randoms(use_true_random=False))
def test_input_mapping_order_cannot_change_the_vector(case, rng: random.Random):
    request, features = case
    columns = list(features.values)
    rng.shuffle(columns)
    shuffled = HistoricalFeatureSet(
        values={name: features.values[name] for name in columns},
        timestamps={name: features.timestamps[name] for name in columns},
    )
    first = ASSEMBLER.assemble(request, features)
    second = ASSEMBLER.assemble(request, shuffled)
    assert (first.columns, first.values) == (second.columns, second.values)


@given(requests_with_features())
def test_assembly_is_deterministic(case):
    request, features = case
    assert ASSEMBLER.assemble(request, features) == ASSEMBLER.assemble(request, features)


@given(requests_with_features())
def test_feature_timestamp_is_the_oldest_contribution(case):
    request, features = case
    observed = [value for value in features.timestamps.values() if value is not None]
    vector = ASSEMBLER.assemble(request, features)
    assert vector.feature_timestamp == min(observed)


@given(requests_with_features())
def test_merchant_columns_are_null_exactly_when_merchant_is_absent(case):
    request, features = case
    by_name = ASSEMBLER.assemble(request, features).as_dict()
    assert by_name["merchant_present"] is (request.merchant_id is not None)
    if request.merchant_id is None:
        assert all(by_name[column] is None for column in MERCHANT_COLUMNS)
        assert by_name["merchant_activity_rate_ratio_10m_vs_24h"] is None


@given(requests_with_features(), st.sampled_from(REQUIRED_COLUMNS))
def test_null_in_a_required_column_is_reported_by_name(case, column):
    request, features = case
    damaged = HistoricalFeatureSet(
        values={**features.values, column: None}, timestamps=features.timestamps
    )
    with pytest.raises(MissingFeatureError) as raised:
        ASSEMBLER.assemble(request, damaged)
    assert raised.value.feature_names == (column,)


@given(requests_with_features(), st.sampled_from(HISTORICAL_FEATURE_COLUMNS))
def test_a_dropped_column_never_produces_a_vector(case, column):
    request, features = case
    damaged = HistoricalFeatureSet(
        values={k: v for k, v in features.values.items() if k != column},
        timestamps=features.timestamps,
    )
    with pytest.raises(FeatureContractError):
        ASSEMBLER.assemble(request, damaged)


@given(requests_with_features())
def test_every_assembled_vector_encodes_to_41_ordered_inputs(case):
    request, features = case
    vector = ASSEMBLER.assemble(request, features)
    encoded = _contract().encode_request(vector, correlation_id="corr")
    assert [item["name"] for item in encoded["inputs"]] == list(MODEL_INPUT_COLUMNS)
    for item, value in zip(encoded["inputs"], vector.values, strict=True):
        assert item["datatype"] == v2_input_datatype(item["name"])
        assert item["shape"] == [1]
        assert item["data"] == [value]
