from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_platform.metadata.datahub.assertions.result_bound_publisher import (
    ASSERTION_TYPE,
    DataHubAdapter,
    _normalized_run_from_raw,
    definition,
    publish_with_readback,
    run_result,
)
from data_platform.metadata.datahub.assertions import recover_validation_results
from data_platform.metadata.datahub.assertions.validation_artifact import (
    ValidationArtifact,
    ValidationOutcome,
)


def outcome(**overrides) -> ValidationOutcome:
    values = {
        "assertion_id": "dq_dp2_transactions_unique",
        "pipeline": "dp2_bronze_to_curated",
        "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:gcs,gs://bucket/silver/transactions,PROD)",
        "dataset_uri": "gs://bucket/silver/transactions",
        "rule_name": "transactions.duplicate_key",
        "description": "No duplicate transaction identifier groups.",
        "status": "PASS",
        "expected": "0 duplicate groups",
        "observed": {"duplicate_group_count": 0},
        "executed_at": "2026-10-25T18:30:07Z",
        "source_run_id": "silver-run-1",
        "source_batch_id": "silver-run-1",
        "source_git_sha": "a" * 40,
        "source_artifact": "/evidence/validation.json",
        "source_artifact_sha256": "sha256:" + "b" * 64,
    }
    values.update(overrides)
    return ValidationOutcome(**values)


def test_executed_result_has_deterministic_identity_and_provenance() -> None:
    item = outcome()
    assert item.assertion_urn == "urn:li:assertion:dq_dp2_transactions_unique"
    assert item.run_id == "dp2_bronze_to_curated:silver-run-1:transactions.duplicate_key"
    assert item.timestamp_millis == 1792953007000
    result = run_result(item)
    assert result["result_type"] == "SUCCESS"
    assert result["assertee_urn"] == item.dataset_urn
    assert result["native_results"]["source_run_id"] == "silver-run-1"
    assert json.loads(result["native_results"]["observed"])["duplicate_group_count"] == 0


def test_failed_check_maps_to_failure_without_rewriting_observed_value() -> None:
    item = outcome(status="FAIL", observed={"duplicate_group_count": 3})
    result = run_result(item)
    assert result["result_type"] == "FAILURE"
    assert json.loads(result["native_results"]["observed"]) == {"duplicate_group_count": 3}


@pytest.mark.parametrize("status", ["NOT_EXECUTED", "BLOCKED"])
def test_non_executed_states_cannot_be_published_as_runs(status: str) -> None:
    item = outcome(
        status=status,
        executed_at="",
        source_run_id="",
        source_artifact="",
        source_artifact_sha256="",
        observed={"reason": "no row-level artifact"},
    )
    with pytest.raises(ValueError, match="no executed result"):
        run_result(item)


def test_invalid_schema_and_missing_provenance_fail_fast(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timestamp, run and evidence"):
        outcome(source_artifact_sha256="")
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps({"schema_version": "unknown", "generated_at": "x", "outcomes": []}))
    with pytest.raises(ValueError, match="unsupported validation artifact"):
        ValidationArtifact.read(path)


class FakeAdapter:
    def __init__(self) -> None:
        self.definitions = {}
        self.runs = {}

    def put_definition(self, item):
        value = definition(item)
        self.definitions[item.assertion_urn] = value
        return {"urn": item.assertion_urn}

    def put_run(self, item):
        value = run_result(item)
        self.runs[(item.assertion_urn, item.run_id)] = value
        return {"urn": item.assertion_urn, "runId": item.run_id}

    def get_definition(self, item):
        value = self.definitions[item.assertion_urn]
        return value, {"raw": {"assertionInfo": value}}

    def get_run(self, item):
        value = self.runs[(item.assertion_urn, item.run_id)]
        return value, {"raw": {"assertionRunEvent": value}}

    def get_run_history_raw(self, item):
        values = [
            value
            for (assertion_urn, _), value in self.runs.items()
            if assertion_urn == item.assertion_urn
        ]
        return {"value": {"values": values}}


def test_publication_is_idempotent_and_verifies_raw_readback() -> None:
    item = outcome()
    artifact = ValidationArtifact((item,), "2026-11-01T00:00:00Z")
    adapter = FakeAdapter()
    first = publish_with_readback(artifact, adapter)
    second = publish_with_readback(artifact, adapter)
    assert first["published_definition_count"] == second["published_definition_count"] == 1
    assert first["published_run_event_count"] == second["published_run_event_count"] == 1
    assert first["verified"] == second["verified"]
    assert first["published"] == 1
    assert first["verified"][0]["raw_definition_api_response"]
    assert first["verified"][0]["raw_run_api_response"]


def test_not_executed_publishes_definition_but_never_a_run() -> None:
    item = outcome(
        status="NOT_EXECUTED",
        executed_at="",
        source_run_id="",
        source_artifact="",
        source_artifact_sha256="",
        observed={"reason": "no row-level artifact"},
    )
    adapter = FakeAdapter()
    result = publish_with_readback(
        ValidationArtifact((item,), "2026-11-01T00:00:00Z"), adapter
    )
    assert result["published_definition_count"] == 1
    assert result["published_run_event_count"] == 0
    assert result["definition_only"][0]["status"] == "NOT_EXECUTED"
    assert adapter.runs == {}


def test_not_executed_fails_if_a_run_event_already_exists() -> None:
    item = outcome(
        status="NOT_EXECUTED",
        executed_at="",
        source_run_id="",
        source_artifact="",
        source_artifact_sha256="",
        observed={"reason": "no row-level artifact"},
    )
    adapter = FakeAdapter()
    adapter.runs[(item.assertion_urn, "fake-run")] = {"result_type": "SUCCESS"}
    with pytest.raises(RuntimeError, match="already contains assertionRunEvent"):
        publish_with_readback(
            ValidationArtifact((item,), "2026-11-01T00:00:00Z"), adapter
        )


def test_semantic_readback_mismatch_fails() -> None:
    item = outcome()
    artifact = ValidationArtifact((item,), "2026-11-01T00:00:00Z")

    class BadAdapter(FakeAdapter):
        def get_run(self, selected):
            value, raw = super().get_run(selected)
            return {**value, "result_type": "FAILURE"}, raw

    with pytest.raises(RuntimeError, match="readback mismatch"):
        publish_with_readback(artifact, BadAdapter())


def test_sdk_definition_readback_normalizes_native_parameter_map() -> None:
    from datahub.metadata.schema_classes import (
        AssertionInfoClass,
        AssertionTypeClass,
        CustomAssertionInfoClass,
    )

    item = outcome()
    expected = definition(item)
    info = AssertionInfoClass(
        type=AssertionTypeClass.CUSTOM,
        description=item.description,
        customAssertion=CustomAssertionInfoClass(
            type=ASSERTION_TYPE,
            entity=item.dataset_urn,
            logic=item.rule_name,
            nativeType=item.rule_name,
            nativeParameters=expected["native_parameters"],
        ),
    )

    class Graph:
        def get_aspect(self, urn, aspect_type):
            assert urn == item.assertion_urn
            return info

        def get_entity_raw(self, urn, aspects):
            return {"urn": urn, "aspects": aspects}

    adapter = DataHubAdapter.__new__(DataHubAdapter)
    adapter.graph = Graph()
    normalized, raw = adapter.get_definition(item)
    assert normalized == expected
    assert raw["aspects"] == ["assertionInfo"]


def test_raw_run_readback_finds_exact_run_without_server_side_filter() -> None:
    item = outcome()
    expected = run_result(item)
    raw = {
        "value": {
            "values": [
                {"aspect": {"value": json.dumps({"runId": "another-run"})}},
                {
                    "aspect": {
                        "value": json.dumps(
                            {
                                "assertionUrn": expected["assertion_urn"],
                                "asserteeUrn": expected["assertee_urn"],
                                "runId": expected["run_id"],
                                "timestampMillis": expected["timestamp_millis"],
                                "result": {
                                    "type": expected["result_type"],
                                    "nativeResults": expected["native_results"],
                                },
                            }
                        )
                    }
                },
            ]
        }
    }
    assert _normalized_run_from_raw(raw, item.run_id) == expected


def test_recovery_generated_at_override_is_byte_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = tmp_path / "inventory.json"
    inventory.write_text("{}\n", encoding="utf-8")
    expected = outcome()
    monkeypatch.setattr(recover_validation_results, "_inventory_lookup", lambda _: {})
    monkeypatch.setattr(recover_validation_results, "recover_dp1", lambda *_: expected)
    monkeypatch.setattr(recover_validation_results, "recover_dp2", lambda *_: ())
    monkeypatch.setattr(
        recover_validation_results,
        "dp2_gold_not_executed",
        lambda *_: outcome(
            assertion_id="dq_dp2_gold_fact_integrity",
            rule_name="fact_transactions.row_level_integrity",
            status="NOT_EXECUTED",
            executed_at="",
            source_run_id="",
            source_batch_id="",
            source_artifact="",
            source_artifact_sha256="",
        ),
    )
    monkeypatch.setattr(recover_validation_results, "recover_dp3", lambda *_: ())
    generated_at = "2026-10-31T21:35:54.049517Z"
    common = [
        "--inventory", str(inventory),
        "--dp1-airflow-log", str(tmp_path / "dp1.log"),
        "--dp2-airflow-log", str(tmp_path / "dp2.log"),
        "--dp3-pit-json", str(tmp_path / "pit.json"),
        "--dp3-execution-json", str(tmp_path / "execution.json"),
        "--generated-at", generated_at,
    ]
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert recover_validation_results.main([*common, "--output", str(first)]) == 0
    assert recover_validation_results.main([*common, "--output", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    assert ValidationArtifact.read(first).generated_at == generated_at
