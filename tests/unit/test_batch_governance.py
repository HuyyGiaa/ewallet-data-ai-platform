"""EDAI-1 Data Governance: verified campaign evidence -> DataHub lineage and assertions.

Every test reads the committed evidence of the canonical 5GB campaign
(``ab-20261110225456``) or a tampered copy of it. DataHub is replaced by in-memory
adapters; nothing here contacts a live service, so a PASS here is an offline
result, never runtime evidence of publication.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from data_platform.metadata.datahub.assertions import campaign_validation, result_bound_publisher
from data_platform.metadata.datahub.assertions.campaign_validation import (
    RuleMappingError,
    campaign_outcomes,
    outcomes_by_stage,
    rule_targets,
)
from data_platform.metadata.datahub.assertions.validation_artifact import ValidationArtifact
from data_platform.metadata.datahub.campaign_evidence import CampaignEvidenceError, load_campaign
from data_platform.metadata.datahub.lineage import batch_lineage, feast_materialization, verified_cloud_publisher
from data_platform.metadata.datahub.lineage.batch_lineage import build_plan, validated_datasets_by_stage
from data_platform.metadata.datahub.urns import dataset_urn, flow_urn, job_urn
from data_platform.contracts.schema_inventory import zone_inventory


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456"
REPORTS = EVIDENCE / "validation_reports"
BENCHMARK = "ab-20261110225456"
DATASET = "rehearsal-5gb-v1"
SOURCE_SHA = "ac2dfb0eac16e173261b3c3f65e83a03d388528538daefd9ec669bef7123eb80"


def campaign(profile: str = "optimized", directory: Path | None = None):
    return load_campaign(directory or EVIDENCE / "manifests" / profile, benchmark_id=BENCHMARK,
                         spark_profile=profile, dataset_version=DATASET, source_manifest_sha256=SOURCE_SHA)


def copied(tmp_path: Path, profile: str = "optimized") -> tuple[Path, Path]:
    manifests = tmp_path / "manifests"
    reports = tmp_path / "reports"
    shutil.copytree(EVIDENCE / "manifests" / profile, manifests)
    shutil.copytree(REPORTS, reports)
    return manifests, reports


def edit_json(path: Path, change) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    change(value)
    path.write_text(json.dumps(value), encoding="utf-8")


class MemoryAdapter:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], dict] = {}
        self.writes = 0

    def put(self, urn, aspect, payload):
        self.writes += 1
        self.values[(urn, aspect)] = json.loads(json.dumps(payload))

    def get(self, urn, aspect):
        return self.values.get((urn, aspect))


# ------------------------------------------------------------------ campaign evidence


@pytest.mark.parametrize("profile", ["baseline", "optimized"])
def test_both_profiles_verify_with_four_successful_stages(profile):
    value = campaign(profile)
    assert [stage.stage for stage in value.stages.values()] == ["bronze", "silver", "gold", "features"]
    assert {stage.dag_id for stage in value.stages.values()} == {
        "dp1_source_to_bronze", "dp2_bronze_to_curated", "dp3_historical_features"}
    assert all(stage.validation_status == "PASS" for stage in value.stages.values())
    assert value.git_sha == "aae22b08eee20fa055c4aefc8d23c14cc3ae5f8c"
    assert value.data_bucket == "ewallet-data-ai-platform-hg26-rehearsal-5gb-data"


def test_an_edited_handoff_breaks_the_checksum_chain(tmp_path):
    manifests, _ = copied(tmp_path)
    edit_json(manifests / "dp1_handoff.json", lambda value: value.update(completed_at="2026-01-01"))
    with pytest.raises(CampaignEvidenceError, match="dp1_handoff.json"):
        campaign(directory=manifests)


def test_an_edited_campaign_manifest_is_refused(tmp_path):
    manifests, _ = copied(tmp_path)
    edit_json(manifests / "campaign_manifest.json", lambda value: value.update(status="COMPLETE "))
    with pytest.raises(CampaignEvidenceError, match="checksum mismatch|status"):
        campaign(directory=manifests)


@pytest.mark.parametrize("override,message", [
    ({"dataset_version": "canonical-100gb-v1"}, "dataset_version"),
    ({"benchmark_id": "ab-20261110215453"}, "benchmark_id"),
    ({"spark_profile": "baseline"}, "spark_profile"),
    ({"source_manifest_sha256": "0" * 64}, "pinned"),
])
def test_the_wrong_identity_is_refused(override, message):
    arguments = {"benchmark_id": BENCHMARK, "spark_profile": "optimized", "dataset_version": DATASET,
                 "source_manifest_sha256": SOURCE_SHA, **override}
    with pytest.raises(CampaignEvidenceError, match=message):
        load_campaign(EVIDENCE / "manifests" / "optimized", **arguments)


# ----------------------------------------------------------------- assertion mapping


def test_every_executed_rule_maps_to_the_dataset_it_checks():
    outcomes = campaign_outcomes(campaign(), REPORTS)
    assert len(outcomes) == 263  # 260 rules; dp3_features.created_shared_by_run covers four tables
    assert {item.status for item in outcomes} == {"PASS"}
    assert len({item.dataset_urn for item in outcomes}) == 34
    per_pipeline = {}
    for item in outcomes:
        per_pipeline[item.pipeline] = per_pipeline.get(item.pipeline, 0) + 1
    assert per_pipeline == {"dp1_source_to_bronze": 54, "dp2_bronze_to_curated": 153, "dp3_historical_features": 56}
    assert len({item.assertion_urn for item in outcomes}) == 263


def test_the_gold_validation_covers_the_scd2_dimension_with_14_rules():
    gold = outcomes_by_stage(campaign(), REPORTS, stages=["gold"])["gold"]
    scd2 = [item for item in gold if item.dataset_uri.endswith("/gold/dim_merchant_scd2")]
    assert len(scd2) == 14
    assert {item.status for item in scd2} == {"PASS"}
    assert all(item.source_batch_id == "v1-20261110225456-optimized-gold-r261110225458" for item in scd2)
    contiguous = next(item for item in scd2 if item.rule_name.endswith("contiguous_non_overlapping_intervals"))
    assert "data_platform/contracts/gold_dim_merchant_scd2.yml" in contiguous.description


def test_gold_scope_merchant_history_rules_target_the_silver_run():
    gold = outcomes_by_stage(campaign(), REPORTS, stages=["gold"])["gold"]
    history = [item for item in gold if item.rule_name.startswith("merchant_history.")]
    assert len(history) == 7
    assert all("-silver-" in item.dataset_uri and item.dataset_uri.endswith("/silver/merchant_history")
               for item in history)
    assert all(item.source_run_id.endswith("gold-r261110225458") for item in history)


def test_cross_table_and_population_rules_are_explicit():
    inventory = zone_inventory(DATASET)
    shared = rule_targets("dp3_features.created_shared_by_run", "features", inventory)
    assert [target.table for target in shared] == list(inventory.features)
    assert all(target.cross_table for target in shared)
    assert rule_targets("silver.users_count_equals_dim_user_count", "core-gold", inventory)[0].table == "dim_user"
    assert rule_targets("fact_transactions_count_equals_obt_transaction_enriched_count", "core-gold",
                        inventory)[0].table == "obt_transaction_enriched"
    assert rule_targets("silver.payment_merchant_timestamp_population_equals_feat_merchant_behavior", "features",
                        inventory)[0].table == "feat_merchant_behavior"


@pytest.mark.parametrize("rule,scope", [("unknown_table.non_empty", "silver"), ("no_table_prefix", "bronze"),
                                        ("dp3_features.x", "core-gold"), ("dim_user.non_empty", "silver")])
def test_an_unmappable_rule_is_refused(rule, scope):
    with pytest.raises(RuleMappingError):
        rule_targets(rule, scope, zone_inventory(DATASET))


def test_a_failed_rule_is_published_as_failure_with_its_observed_value(tmp_path):
    manifests, reports = copied(tmp_path)

    def fail_one(value):
        value["results"][0]["passed"] = False
        value["results"][0]["actual_value"] = "3"
        value["failed_rules"] = [value["results"][0]["rule_name"]]
        value["passed_rules"] -= 1
        value["status"] = "FAIL"

    edit_json(reports / "optimized-bronze.json", fail_one)
    with pytest.raises(CampaignEvidenceError, match="status"):
        campaign_outcomes(campaign(directory=manifests), reports)  # the manifest still says PASS
    loaded = campaign(directory=manifests)
    stage = loaded.stages["bronze"]
    import dataclasses
    failed_campaign = dataclasses.replace(loaded, stages={**loaded.stages,
                                                          "bronze": dataclasses.replace(stage, validation_status="FAIL")})
    outcomes = outcomes_by_stage(failed_campaign, reports, stages=["bronze"])["bronze"]
    failure = next(item for item in outcomes if item.status == "FAIL")
    assert failure.observed["actual_value"] == "3"
    assert result_bound_publisher.run_result(failure)["result_type"] == "FAILURE"


def test_a_result_without_a_boolean_is_unknown_and_never_a_run(tmp_path):
    manifests, reports = copied(tmp_path)

    def unknown(value):
        value["results"][1]["passed"] = None
        value["passed_rules"] -= 1

    edit_json(reports / "optimized-silver.json", unknown)
    outcomes = outcomes_by_stage(campaign(directory=manifests), reports, stages=["silver"])["silver"]
    item = next(outcome for outcome in outcomes if outcome.status == "UNKNOWN")
    assert item.executed_at == "" and item.source_artifact == ""
    with pytest.raises(ValueError, match="no executed result"):
        result_bound_publisher.run_result(item)


def test_an_unexecuted_stage_yields_not_executed_outcomes_only():
    import dataclasses
    loaded = campaign()
    stage = dataclasses.replace(loaded.stages["features"], validation_status=None, validation_batch_id=None,
                                validation_report_uri=None)
    pending = dataclasses.replace(loaded, stages={**loaded.stages, "features": stage})
    outcomes = outcomes_by_stage(pending, REPORTS, stages=["features"])["features"]
    assert len(outcomes) == 4 and {item.status for item in outcomes} == {"NOT_EXECUTED"}


@pytest.mark.parametrize("field,value", [("run_id", "ab-other-run"), ("dataset_version", "canonical-100gb-v1"),
                                         ("scope", "silver"), ("bronze_root", "gs://elsewhere/bronze")])
def test_a_report_of_another_run_is_refused(tmp_path, field, value):
    manifests, reports = copied(tmp_path)
    edit_json(reports / "optimized-bronze.json", lambda document: document.update({field: value}))
    with pytest.raises(CampaignEvidenceError, match=field):
        campaign_outcomes(campaign(directory=manifests), reports, stages=["bronze"])


def test_a_missing_report_is_an_error_not_a_silent_skip(tmp_path):
    manifests, reports = copied(tmp_path)
    (reports / "optimized-gold.json").unlink()
    with pytest.raises(CampaignEvidenceError, match="no report"):
        campaign_outcomes(campaign(directory=manifests), reports)


def test_the_artifact_is_byte_stable_and_dry_runs_through_the_existing_publisher(tmp_path):
    arguments = ["--manifest-dir", str(EVIDENCE / "manifests" / "optimized"), "--reports-dir", str(REPORTS),
                 "--benchmark-id", BENCHMARK, "--spark-profile", "optimized", "--dataset-version", DATASET,
                 "--source-manifest-sha256", SOURCE_SHA, "--stage", "gold"]
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    assert campaign_validation.main([*arguments, "--output", str(first)]) == 0
    assert campaign_validation.main([*arguments, "--output", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    assert len(ValidationArtifact.read(first).outcomes) == 93
    dry = tmp_path / "dry.json"
    assert result_bound_publisher.main(["--validation-artifact", str(first), "--output", str(dry), "--dry-run"]) == 0
    payload = json.loads(dry.read_text(encoding="utf-8"))
    assert len(payload["runs"]) == 93 and payload["not_published"] == []
    assert {run["result_type"] for run in payload["runs"]} == {"SUCCESS"}


# --------------------------------------------------------------------------- lineage


@pytest.fixture(scope="module")
def plan():
    value = campaign()
    return build_plan(value, validated_datasets=validated_datasets_by_stage(value, REPORTS))


def _key(urn: str) -> str:
    uri = urn.split(",", 1)[1].rsplit(",", 1)[0]
    if "/source/" in uri:
        return "source." + uri.rsplit("/", 1)[1]
    zone_folder, table = uri.rsplit("/", 2)[1:]
    zone = "features" if "-feat-" in uri else zone_folder
    return f"{zone}.{table}"


def test_plan_counts(plan):
    assert (plan["dataset_count"], plan["flow_count"], plan["job_count"], plan["lineage_count"]) == (44, 3, 8, 34)
    assert plan["schema_source"].startswith("none")


def test_dataset_edges_follow_the_builders(plan):
    edges = {_key(edge["downstream"]): sorted(map(_key, edge["upstreams"])) for edge in plan["lineage"]}
    assert edges["bronze.transactions"] == ["source.transactions_v1", "source.transactions_v2"]
    assert edges["bronze.merchant_history"] == ["source.merchant_history"]
    assert edges["silver.merchant_history"] == ["bronze.merchant_history"]
    assert edges["gold.dim_merchant_scd2"] == ["silver.merchant_history"]
    assert edges["gold.dim_merchant"] == ["silver.merchants"]
    assert edges["gold.dim_date"] == ["silver.balance_snapshots", "silver.login_events", "silver.transactions"]
    assert edges["gold.obt_transaction_enriched"] == ["gold.dim_account", "gold.dim_date", "gold.dim_device",
                                                       "gold.dim_merchant", "gold.dim_user", "gold.fact_transactions"]
    assert edges["features.feat_device_behavior"] == ["silver.devices", "silver.transactions"]
    # No Gold or feature builder reads Silver fraud labels.
    curated = {key: value for key, value in edges.items() if key.startswith(("gold.", "features."))}
    assert all("silver.fraud_labels" not in value for value in curated.values())


def test_jobs_are_the_airflow_tasks_that_ran_with_exact_inputs(plan):
    jobs = {(job["dag_id"], job["task_id"]): job for job in plan["jobs"]}
    assert set(jobs) == {(dag, f"{stage}.{task}") for dag, stage in (
        ("dp1_source_to_bronze", "bronze"), ("dp2_bronze_to_curated", "silver"), ("dp2_bronze_to_curated", "gold"),
        ("dp3_historical_features", "features")) for task in ("submit_transformation", "semantic_validation")}
    dp3 = jobs[("dp3_historical_features", "features.submit_transformation")]
    assert sorted(map(_key, dp3["inputs"])) == ["silver.devices", "silver.transactions"]
    assert len(dp3["outputs"]) == 4
    gold = jobs[("dp2_bronze_to_curated", "gold.submit_transformation")]
    assert "silver.fraud_labels" not in map(_key, gold["inputs"])
    assert "silver.merchant_history" in map(_key, gold["inputs"])
    gold_check = jobs[("dp2_bronze_to_curated", "gold.semantic_validation")]
    assert gold_check["outputs"] == [] and "silver.merchant_history" in map(_key, gold_check["inputs"])
    assert gold_check["job_custom_properties"]["validation_status"] == "PASS"
    assert jobs[("dp1_source_to_bronze", "bronze.submit_transformation")]["job_custom_properties"][
        "dataproc_batch_id"] == "ab-20261110225456-optimized-bronze-r261110225456"


def test_every_entity_carries_the_campaign_identity(plan):
    for record in verified_cloud_publisher.records(plan):
        urn, aspect, payload = record
        if aspect in ("datasetProperties", "dataFlowInfo", "dataJobInfo"):
            assert payload["customProperties"]["benchmark_id"] == BENCHMARK
            assert payload["customProperties"]["spark_profile"] == "optimized"
            assert payload["customProperties"]["source_manifest_sha256"] == SOURCE_SHA


def test_publication_is_idempotent_and_fails_closed(plan):
    adapter = MemoryAdapter()
    first = verified_cloud_publisher.publish_with_readback(plan, adapter)
    second = verified_cloud_publisher.publish_with_readback(plan, adapter)
    assert first["published"] == second["published"] == 97
    assert len(adapter.values) == 97

    class Drifting(MemoryAdapter):
        def get(self, urn, aspect):
            value = super().get(urn, aspect)
            if aspect == "upstreamLineage":
                value = {"upstreams": value["upstreams"][:-1]}
            return value

    with pytest.raises(RuntimeError, match="readback mismatch"):
        verified_cloud_publisher.publish_with_readback(plan, Drifting())


def test_an_undeclared_dataset_or_foreign_document_is_refused(plan):
    broken = json.loads(json.dumps(plan))
    broken["jobs"][0]["inputs"].append(dataset_urn("gs://elsewhere/table"))
    with pytest.raises(ValueError, match="undeclared"):
        verified_cloud_publisher.records(broken)
    with pytest.raises(ValueError, match="VERIFIED_CAMPAIGN_EVIDENCE"):
        verified_cloud_publisher.records({**plan, "mode": "READ_ONLY_REAL_CLOUD_DISCOVERY"})


def test_an_observed_schema_of_another_campaign_is_refused():
    with pytest.raises(ValueError, match="benchmark_id"):
        build_plan(campaign(), observed={"provenance": {"dataset_version": DATASET, "benchmark_id": "other"}})


def test_observed_schemas_become_schema_metadata():
    value = campaign()
    inventory = zone_inventory(DATASET)
    observed = {
        "provenance": {**value.identity()},
        "source": {"tables": {name: {"columns": [{"name": "id", "type": "string", "nullable": True}]}
                              for name in inventory.source_physical}},
        "zones": {zone: {"tables": {table: {"columns": [{"name": "id", "type": "string", "nullable": True}],
                                            "partition_columns": [], "delta_version": 0}
                                    for table in inventory.tables(zone)}}
                  for zone in ("bronze", "silver", "gold", "features")},
    }
    records = verified_cloud_publisher.records(build_plan(value, observed=observed))
    assert sum(aspect == "schemaMetadata" for _, aspect, _ in records) == 44


def test_dry_run_is_the_default_and_never_constructs_the_adapter(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("the DataHub adapter must not be constructed in a dry run")

    monkeypatch.setattr(verified_cloud_publisher, "DataHubAdapter", forbidden)
    output = tmp_path / "plan.json"
    assert batch_lineage.main(["--manifest-dir", str(EVIDENCE / "manifests" / "optimized"),
                               "--reports-dir", str(REPORTS), "--benchmark-id", BENCHMARK,
                               "--spark-profile", "optimized", "--dataset-version", DATASET,
                               "--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == "DRY_RUN" and payload["record_count"] == 97
    with pytest.raises(SystemExit):
        batch_lineage.main(["--manifest-dir", str(EVIDENCE / "manifests" / "optimized"),
                            "--reports-dir", str(REPORTS), "--benchmark-id", BENCHMARK,
                            "--spark-profile", "optimized", "--dataset-version", DATASET,
                            "--output", str(output), "--publish"])


# ------------------------------------------------------ Feast materialization contract


def test_batch_and_feast_publishers_share_one_urn_convention():
    assert feast_materialization._flow_urn("feast_incremental_materialization") == flow_urn(
        "feast_incremental_materialization")
    assert feast_materialization._job_urn("dp3_historical_features") == job_urn(
        "dp3_historical_features", "materialize_window")
    uri = "gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data/features/offline/snapshots/x"
    assert feast_materialization._dataset_urn("gcs", uri) == dataset_urn(uri)
    assert verified_cloud_publisher.flow_urn is flow_urn


def test_batch_lineage_never_names_a_feast_entity(plan):
    names = {dataset["qualified_name"] for dataset in plan["datasets"]}
    assert not any("/features/offline/" in name or name.startswith("feast://") for name in names)
    assert {job["dag_id"] for job in plan["jobs"]}.isdisjoint({"feast_incremental_materialization"})
