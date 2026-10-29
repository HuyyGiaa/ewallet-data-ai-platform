from data_platform.storage.scripts.warehouse_benchmark import QUERY, _median


def test_benchmark_query_has_prunable_predicates():
    assert "event_date BETWEEN" in QUERY
    assert "merchant_id = 42" in QUERY
    assert "status = 'completed'" in QUERY


def test_median_is_deterministic():
    assert _median([4.0, 1.0, 3.0, 2.0, 100.0]) == 3.0
