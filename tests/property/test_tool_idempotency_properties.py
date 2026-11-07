"""Read-only MCP tools answer identically for identical calls.

Tools have no idempotency store: the contract is referential transparency for
a fixed backend. The executor and tool code are real; every backend is the
fixture already used by the Phase D suite.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS
from tests.llm_agent.test_phase_d_tools import context, feature_rag, run, transaction
from tests.property.strategies import duplicate_counts, positive_amounts, short_ids


TOOLS = feature_rag()
feature_subsets = st.lists(
    st.sampled_from(HISTORICAL_FEATURE_COLUMNS),
    min_size=1,
    max_size=len(HISTORICAL_FEATURE_COLUMNS),
    unique=True,
)
queries = st.sampled_from(
    ["account takeover", "payment velocity", "merchant policy", "device risk"]
)
transactions = st.builds(
    lambda identifier, amount: transaction(transaction_id=identifier, amount=amount),
    short_ids,
    positive_amounts,
)


def call(name: str, arguments: dict) -> dict:
    """Invoke one tool and return its response as a dictionary."""
    return run(TOOLS.call(name, arguments, context())).to_dict()


def assert_repeatable(name: str, arguments: dict, repeats: int) -> dict:
    """Require ``repeats`` further calls to equal the first and return it."""
    first = call(name, arguments)
    for _ in range(repeats):
        assert call(name, arguments) == first
    return first


@given(transactions, feature_subsets, duplicate_counts)
def test_feature_lookup_is_repeatable_and_projects_the_request(
    tx, feature_names, repeats
):
    response = assert_repeatable(
        "feature_lookup",
        {"transaction": tx, "feature_names": feature_names},
        repeats,
    )
    assert response["status"] == "ok"
    assert list(response["result"]["historical_features"]) == feature_names


@given(transactions, duplicate_counts)
def test_fraud_scoring_is_repeatable(tx, repeats):
    response = assert_repeatable("fraud_scoring", {"transaction": tx}, repeats)
    assert response["status"] == "ok"


@given(queries, st.integers(min_value=1, max_value=4), duplicate_counts)
def test_knowledge_search_is_repeatable(query, top_k, repeats):
    response = assert_repeatable(
        "knowledge_search", {"query": query, "top_k": top_k}, repeats
    )
    assert response["status"] == "ok"


@given(
    transactions,
    st.lists(st.text(min_size=1, max_size=24), min_size=1, max_size=4).filter(
        lambda names: not set(names) <= set(HISTORICAL_FEATURE_COLUMNS)
    ),
)
def test_unknown_features_are_rejected_deterministically(tx, feature_names):
    arguments = {"transaction": tx, "feature_names": feature_names}
    response = assert_repeatable("feature_lookup", arguments, 1)
    assert response["status"] == "rejected"
