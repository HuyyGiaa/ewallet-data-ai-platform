from data_platform.generation.src.sharded.profile_manifest import build_profile


def test_manifest_profile_measures_only_supported_metrics():
    plan = {"digest": "abc", "global_seed": 7}
    manifest = {
        "status": "COMPLETE", "plan_digest": "abc", "dataset_version": "v1",
        "shard_count": 1, "total_bytes": 101, "logical_transactions": 100,
        "physical_transactions": 102,
        "tables": {
            "transactions_v1": {"rows": 40, "schema_sha256": ["v1"]},
            "transactions_v2": {"rows": 62, "schema_sha256": ["v2"]},
        },
    }
    result = build_profile(plan, manifest)
    assert result["duplicate_rate_percent"] == 2.0
    assert result["schema_evolution"]["different_schema_hash"]
    assert result["column_distributions"]["merchant_skew_top_n_share"]["status"] == "PENDING_COLUMN_SCAN"
