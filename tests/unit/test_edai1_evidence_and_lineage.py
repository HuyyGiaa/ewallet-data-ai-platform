from pathlib import Path

from data_platform.metadata.datahub.lineage.cloud_lineage import build_plan
from data_platform.metadata.datahub.lineage.cloud_publisher import (
    aspect_records,
    publish_with_readback,
)
from data_platform.orchestration.airflow.evidence import collect, parse_task_log


def test_cloud_lineage_plan_has_stable_operational_entities():
    plan = build_plan()
    assert plan["flow_count"] == 3
    assert plan["job_count"] == 4
    assert plan["dataset_count"] == 32
    assert plan["dataset_lineage_count"] == 31
    jobs = plan["jobs"]
    assert len({job["job_urn"] for job in jobs}) == 4
    assert all(job["inputs"] and job["outputs"] for job in jobs)
    assert all("gs://ewallet-data-ai-platform-hg26-data/" in urn for urn in plan["datasets"])
    assert all("urn:li:dataFlow:(airflow," in job["flow_urn"] for job in jobs)
    assert len({edge["downstream"] for edge in plan["dataset_lineage"]}) == 31
    feature_edge = next(
        edge for edge in plan["dataset_lineage"]
        if "feat_device_behavior" in edge["downstream"]
    )
    assert len(feature_edge["upstreams"]) == 2


def test_publisher_is_idempotent_and_fails_closed_on_readback():
    class MemoryAdapter:
        def __init__(self):
            self.values = {}
        def put(self, urn, aspect, payload):
            self.values[(urn, aspect)] = payload
        def get(self, urn, aspect):
            return self.values.get((urn, aspect))

    plan = build_plan()
    adapter = MemoryAdapter()
    first = publish_with_readback(plan, adapter)
    second = publish_with_readback(plan, adapter)
    assert first["published"] == second["published"] == 42
    assert len(aspect_records(plan)) == 42

    class BadReadback(MemoryAdapter):
        def get(self, urn, aspect):
            return None
    import pytest
    with pytest.raises(RuntimeError, match="readback mismatch"):
        publish_with_readback(plan, BadReadback())


def test_airflow_log_collector_is_read_only_and_hashes_evidence(tmp_path: Path):
    path = (
        tmp_path / "dag_id=dp1_source_to_bronze" / "run_id=sample" /
        "task_id=submit_bronze_dataproc" / "attempt=1.log"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        "[2026-10-25T13:44:23.315439Z] start "
        "ab-20261025134342-baseline-bronze-r261025134410\n"
        "[2026-10-25T14:15:51.642676Z] Command exited with return code 0\n"
        "Marking task as SUCCESS\n",
        encoding="utf-8",
    )
    item = parse_task_log(path)
    assert item is not None
    assert item.success_marker
    assert item.final_return_code == 0
    assert len(item.sha256) == 64
    assert item.dataproc_batch_ids == (
        "ab-20261025134342-baseline-bronze-r261025134410",
    )
    result = collect(tmp_path)
    assert result["state_counts"] == {"success": 1}
    assert result["run_count"] == 1


def test_airflow_collector_parses_structured_airflow_three_log(tmp_path: Path):
    path = (
        tmp_path / "dag_id=dp2_bronze_to_curated" / "run_id=structured" /
        "task_id=verify_silver_delta" / "attempt=2.log"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"timestamp":"2026-10-25T15:25:37.876521Z",'
        '"event":"Command exited with return code 0"}\n'
        '{"timestamp":"2026-10-25T15:25:44.287522Z",'
        '"event":"Task instance in success state"}\n',
        encoding="utf-8",
    )
    item = parse_task_log(path)
    assert item is not None
    assert item.first_timestamp == "2026-10-25T15:25:37.876521Z"
    assert item.last_timestamp == "2026-10-25T15:25:44.287522Z"
    assert item.success_marker
