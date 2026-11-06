"""F4D derived-feature arithmetic on multi-row data.

The online path only ever passes one row and later maps ``inf`` to null, which
would hide a wrong denominator guard. Training passes whole frames, so the
shared helpers are tested directly against the plain-Python oracle.
"""

from __future__ import annotations

import math

from hypothesis import given, strategies as st
import pandas as pd

from ai_platform.ml.training.contract import DERIVED_FEATURE_COLUMNS
from ai_platform.ml.training.derived import add_derived_features, safe_divide


finite_numbers = st.floats(
    min_value=-1e12, max_value=1e12, allow_nan=False, allow_infinity=False
)
optional_numbers = st.none() | st.sampled_from([0.0, 1.0, -1.0]) | finite_numbers
number_pairs = st.lists(st.tuples(optional_numbers, optional_numbers), min_size=1, max_size=12)
garbage = st.text("abcxyz", min_size=1, max_size=5)


def as_list(series: pd.Series) -> list[float | None]:
    """Convert a float series to a list, with NaN as ``None``."""
    return [None if math.isnan(value) else float(value) for value in series]


def ieee_ratio(numerator: float | None, denominator: float | None) -> float | None:
    """State the shared helper's contract: only absent inputs and zero are missing.

    An overflowing quotient stays infinite here. Mapping it to null belongs to
    the online assembler and is checked by ``assertions.safe_ratio``.
    """
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def assert_matches_oracle(observed: list[float | None], pairs) -> None:
    """Compare each result with the plain-Python ratio."""
    for value, (numerator, denominator) in zip(observed, pairs, strict=True):
        expected = ieee_ratio(numerator, denominator)
        if expected is None:
            assert value is None
        else:
            assert math.isclose(value, expected, rel_tol=1e-12)


@given(number_pairs)
def test_safe_divide_matches_the_oracle_row_by_row(pairs):
    numerators, denominators = zip(*pairs)
    result = safe_divide(
        pd.Series(numerators, dtype="float64"),
        pd.Series(denominators, dtype="float64"),
    )
    assert result.dtype == "float64"
    assert len(result) == len(pairs)
    assert_matches_oracle(as_list(result), pairs)


@given(number_pairs, st.integers(min_value=1, max_value=1_000))
def test_safe_divide_keeps_the_input_index(pairs, start):
    numerators, denominators = zip(*pairs)
    index = range(start, start + len(pairs))
    result = safe_divide(
        pd.Series(numerators, index=index, dtype="float64"),
        pd.Series(denominators, index=index, dtype="float64"),
    )
    assert list(result.index) == list(index)


def test_an_overflowing_quotient_stays_infinite_in_the_shared_helper():
    result = safe_divide(pd.Series([1.0]), pd.Series([2.225073858507203e-309]))
    assert as_list(result) == [math.inf]


@given(finite_numbers.filter(lambda value: value != 0))
def test_division_by_zero_is_missing_never_infinite(numerator):
    result = safe_divide(pd.Series([numerator]), pd.Series([0.0]))
    assert as_list(result) == [None]


@given(finite_numbers)
def test_division_by_one_is_the_identity(numerator):
    assert as_list(safe_divide(pd.Series([numerator]), pd.Series([1.0]))) == [numerator]


@given(garbage, finite_numbers.filter(lambda value: value != 0))
def test_non_numeric_operands_become_missing(text, number):
    assert as_list(safe_divide(pd.Series([text]), pd.Series([number]))) == [None]
    assert as_list(safe_divide(pd.Series([number]), pd.Series([text]))) == [None]


@given(st.lists(st.integers(1, 10_000), min_size=1, max_size=6))
def test_add_derived_features_preserves_the_frame(counts):
    frame = pd.DataFrame(
        {
            "amount": [float(count) for count in counts],
            "user_behavior__user_avg_amount_30d": 2.0,
            "user_behavior__user_std_amount_30d": 4.0,
            "account_behavior__account_avg_amount_30d": 8.0,
            "account_behavior__account_std_amount_30d": 16.0,
            "account_behavior__account_tx_count_1h": 3,
            "user_behavior__user_tx_count_1h": 6,
            "merchant_behavior__merchant_tx_count_10m": 2,
            "merchant_behavior__merchant_tx_count_24h": 288,
        }
    )
    original = frame.copy()
    result = add_derived_features(frame)

    pd.testing.assert_frame_equal(frame, original)
    assert set(DERIVED_FEATURE_COLUMNS) <= set(result.columns)
    assert list(result["amount_ratio_to_user_avg_30d"]) == [c / 2.0 for c in counts]
    assert list(result["amount_zscore_user_30d"]) == [(c - 2.0) / 4.0 for c in counts]
    assert list(result["amount_ratio_to_account_avg_30d"]) == [c / 8.0 for c in counts]
    assert list(result["amount_zscore_account_30d"]) == [(c - 8.0) / 16.0 for c in counts]
    assert set(result["account_tx_share_1h"]) == {0.5}
    assert set(result["merchant_activity_rate_ratio_10m_vs_24h"]) == {1.0}
