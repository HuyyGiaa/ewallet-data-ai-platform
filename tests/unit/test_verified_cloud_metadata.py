import copy

import pytest

from data_platform.metadata.datahub.lineage.cloud_discovery import _select_batches
from data_platform.metadata.datahub.lineage.verified_cloud_publisher import (
    flow_urn,
    job_urn,
    publish_with_readback,
    records,
)


def _batch(component: str) -> dict:
    return {
        "batch_id": f"batch-{component}",
        "state": "SUCCEEDED",
        "labels": {"component": component, "spark_profile": "optimized"},
        "args": ["--git-sha=abc123"],
    }


def _inventory() -> dict:
    source = "urn:li:dataset:(urn:li:dataPlatform:gcs,gs://bucket/source/users,PROD)"
    bronze = "urn:li:dataset:(urn:li:dataPlatform:gcs,gs://bucket/bronze/users,PROD)"
    datasets = []
    for zone, urn in (("source", source), ("bronze", bronze)):
        datasets.append({
            "urn": urn,
            "uri": urn.split(",", 1)[1].rsplit(",", 1)[0],
            "zone": zone,
            "table": "users",
            "format": "PARQUET" if zone == "source" else "DELTA",
            "physical_exists": True,
            "schema_fields": [{"name": "user_id", "type": "string", "nullable": False}],
            "partition_columns": [],
            "file_count": 1,
        })
    return {
        "mode": "READ_ONLY_REAL_CLOUD_DISCOVERY",
        "dataset_count": 2,
        "source_git_sha": "abc123",
        "datasets": datasets,
        "jobs": [{
            "dag_id": "dp1_source_to_bronze",
            "task_id": "submit_bronze_dataproc",
            "batch_id": "batch-source-bronze",
            "batch_state": "SUCCEEDED",
            "git_sha": "abc123",
            "inputs": [source],
            "outputs": [bronze],
        }],
        "lineage": [{"downstream": bronze, "upstreams": [source]}],
    }


class MemoryAdapter:
    def __init__(self) -> None:
        self.values = {}

    def put(self, urn, aspect, payload) -> None:
        self.values[(urn, aspect)] = payload

    def get(self, urn, aspect):
        return self.values.get((urn, aspect))


def test_optimized_batch_selection_requires_all_components_and_one_git_sha():
    values = [_batch(component) for component in (
        "source-bronze", "bronze-silver", "core-gold", "historical-features"
    )]
    assert set(_select_batches(values)) == {
        "source-bronze", "bronze-silver", "core-gold", "historical-features"
    }
    values[-1]["args"] = ["--git-sha=different"]
    with pytest.raises(ValueError, match="Git SHA mismatch"):
        _select_batches(values)


def test_verified_records_and_semantic_readback_cover_dataset_job_and_lineage():
    inventory = _inventory()
    generated = records(inventory)
    # 2 properties + 2 schemas + flow + job + job I/O + lineage.
    assert len(generated) == 8
    assert (flow_urn("dp1_source_to_bronze"), "dataFlowInfo") in {
        (urn, aspect) for urn, aspect, _ in generated
    }
    assert (job_urn("dp1_source_to_bronze", "submit_bronze_dataproc"), "dataJobInputOutput") in {
        (urn, aspect) for urn, aspect, _ in generated
    }
    assert publish_with_readback(inventory, MemoryAdapter())["published"] == 8


def test_publisher_fails_closed_for_unverified_lineage_or_synthetic_inventory():
    inventory = _inventory()
    inventory["mode"] = "SYNTHETIC"
    with pytest.raises(ValueError, match="real cloud discovery"):
        records(inventory)

    inventory = _inventory()
    broken = copy.deepcopy(inventory)
    broken["lineage"][0]["upstreams"] = ["urn:li:dataset:missing"]
    with pytest.raises(ValueError, match="undiscovered"):
        records(broken)
