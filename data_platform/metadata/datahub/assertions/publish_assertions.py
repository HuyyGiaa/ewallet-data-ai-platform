"""Evaluate selected data-contract assertions and publish them to DataHub.

The Silver and Gold validators remain the complete fail-hard validation layer.
This script exposes a small, representative subset as DataHub custom
assertions. All evaluations are read-only Trino queries.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datahub.emitter.mce_builder import make_assertion_urn
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    AssertionResultClass,
    AssertionResultTypeClass,
    AssertionRunEventClass,
    AssertionRunStatusClass,
)
from datahub.metadata.urns import DataPlatformUrn, DatasetUrn
from datahub.sdk import DataHubClient
from trino.dbapi import connect

from data_platform.storage.scripts.config import (
    TRINO_CATALOG,
    TRINO_HOST,
    TRINO_PORT,
    TRINO_USER,
)


DATAHUB_ENV = "PROD"
DEFAULT_DATAHUB_GMS_URL = "http://localhost:8080"
ASSERTION_TYPE = "E-Wallet Data Contract"
ASSERTION_PLATFORM_URN = str(DataPlatformUrn("trino"))


@dataclass(frozen=True)
class Evaluation:
    passed: bool
    observed: dict[str, object]


Evaluator = Callable[[Sequence[object]], Evaluation]


@dataclass(frozen=True)
class AssertionSpec:
    assertion_id: str
    dataset_name: str
    description: str
    expected: str
    contract_path: str
    validator_rule: str
    cost: str
    query: str
    evaluator: Evaluator
    field_paths: tuple[str, ...] = ()

    @property
    def dataset_urn(self) -> str:
        return str(
            DatasetUrn(
                platform="trino",
                name=self.dataset_name,
                env=DATAHUB_ENV,
            )
        )

    @property
    def assertion_urn(self) -> str:
        return make_assertion_urn(self.assertion_id)


def evaluate_zero(row: Sequence[object]) -> Evaluation:
    invalid_count = int(row[0])
    return Evaluation(
        passed=invalid_count == 0,
        observed={"invalid_count": invalid_count},
    )


def evaluate_equal_counts(row: Sequence[object]) -> Evaluation:
    upstream_count = int(row[0])
    downstream_count = int(row[1])
    return Evaluation(
        passed=upstream_count == downstream_count,
        observed={
            "upstream_count": upstream_count,
            "downstream_count": downstream_count,
        },
    )


def evaluate_rate_range(row: Sequence[object]) -> Evaluation:
    invalid_count = int(row[0])
    return Evaluation(
        passed=invalid_count == 0,
        observed={
            "invalid_count": invalid_count,
            "minimum": row[1],
            "maximum": row[2],
        },
    )


def evaluate_exact_coverage(row: Sequence[object]) -> Evaluation:
    orphan_count = int(row[0])
    missing_count = int(row[1])
    label_count = int(row[2])
    transaction_count = int(row[3])
    return Evaluation(
        passed=(
            orphan_count == 0
            and missing_count == 0
            and label_count == transaction_count
        ),
        observed={
            "orphan_count": orphan_count,
            "missing_count": missing_count,
            "label_count": label_count,
            "transaction_count": transaction_count,
        },
    )


REQUIRED_TRANSACTION_FIELDS = (
    "transaction_id",
    "account_id",
    "user_id",
    "device_id",
    "type",
    "amount",
    "currency",
    "status",
    "channel",
    "old_balance",
    "new_balance",
    "timestamp",
    "ingested_at",
    "event_date",
)


ASSERTIONS = (
    AssertionSpec(
        assertion_id="silver_transactions_required_fields_non_null",
        dataset_name="delta.silver_zone.transactions",
        description=(
            "Required Silver transaction fields contain no null values. "
            "merchant_id and counterparty_account_id remain optional."
        ),
        expected="invalid required-field row count = 0",
        contract_path="data_platform/contracts/silver_transactions.yml",
        validator_rule="transactions.required_columns_not_null",
        cost="MEDIUM",
        query="""
            SELECT count_if(
                transaction_id IS NULL
                OR account_id IS NULL
                OR user_id IS NULL
                OR device_id IS NULL
                OR type IS NULL
                OR amount IS NULL
                OR currency IS NULL
                OR status IS NULL
                OR channel IS NULL
                OR old_balance IS NULL
                OR new_balance IS NULL
                OR timestamp IS NULL
                OR ingested_at IS NULL
                OR event_date IS NULL
            )
            FROM delta.silver_zone.transactions
        """,
        evaluator=evaluate_zero,
        field_paths=REQUIRED_TRANSACTION_FIELDS,
    ),
    AssertionSpec(
        assertion_id="silver_transactions_unique_transaction_id",
        dataset_name="delta.silver_zone.transactions",
        description="Silver transactions contain no duplicate transaction_id groups.",
        expected="duplicate transaction_id group count = 0",
        contract_path="data_platform/contracts/silver_transactions.yml",
        validator_rule="transactions.duplicate_key",
        cost="HIGH",
        query="""
            SELECT count(*)
            FROM (
                SELECT transaction_id
                FROM delta.silver_zone.transactions
                GROUP BY transaction_id
                HAVING count(*) > 1
            ) duplicate_groups
        """,
        evaluator=evaluate_zero,
        field_paths=("transaction_id",),
    ),
    AssertionSpec(
        assertion_id="silver_transactions_account_fk_has_no_orphans",
        dataset_name="delta.silver_zone.transactions",
        description=(
            "Every non-null Silver transaction account_id exists in "
            "silver.accounts."
        ),
        expected="orphan account_id count = 0",
        contract_path="data_platform/contracts/silver_transactions.yml",
        validator_rule="transactions.account_id_exists_in_accounts",
        cost="HIGH",
        query="""
            SELECT count(*)
            FROM (
                SELECT DISTINCT transactions.account_id
                FROM delta.silver_zone.transactions transactions
                LEFT JOIN delta.silver_zone.accounts accounts
                    ON transactions.account_id = accounts.account_id
                WHERE transactions.account_id IS NOT NULL
                  AND accounts.account_id IS NULL
            ) orphan_accounts
        """,
        evaluator=evaluate_zero,
        field_paths=("account_id",),
    ),
    AssertionSpec(
        assertion_id="gold_fact_population_matches_silver_transactions",
        dataset_name="delta.gold_zone.fact_transactions",
        description=(
            "Gold fact_transactions preserves the current Silver transaction "
            "population."
        ),
        expected="silver.transactions row count = gold.fact_transactions row count",
        contract_path="data_platform/contracts/gold_fact_transactions.yml",
        validator_rule="silver.transactions_count_equals_fact_transactions_count",
        cost="MEDIUM",
        query="""
            SELECT
                (SELECT count(*) FROM delta.silver_zone.transactions),
                (SELECT count(*) FROM delta.gold_zone.fact_transactions)
        """,
        evaluator=evaluate_equal_counts,
    ),
    AssertionSpec(
        assertion_id="gold_obt_population_matches_fact_transactions",
        dataset_name="delta.gold_zone.obt_transaction_enriched",
        description=(
            "Gold transaction OBT preserves the current fact_transactions "
            "population."
        ),
        expected=(
            "gold.fact_transactions row count = "
            "gold.obt_transaction_enriched row count"
        ),
        contract_path="data_platform/contracts/gold_obt_transaction_enriched.yml",
        validator_rule=(
            "fact_transactions_count_equals_obt_transaction_enriched_count"
        ),
        cost="MEDIUM",
        query="""
            SELECT
                (SELECT count(*) FROM delta.gold_zone.fact_transactions),
                (SELECT count(*) FROM delta.gold_zone.obt_transaction_enriched)
        """,
        evaluator=evaluate_equal_counts,
    ),
    AssertionSpec(
        assertion_id="gold_obt_unique_transaction_id",
        dataset_name="delta.gold_zone.obt_transaction_enriched",
        description=(
            "Gold transaction OBT contains no duplicate transaction_id groups."
        ),
        expected="duplicate transaction_id group count = 0",
        contract_path="data_platform/contracts/gold_obt_transaction_enriched.yml",
        validator_rule="obt_transaction_enriched.unique_key(transaction_id)",
        cost="HIGH",
        query="""
            SELECT count(*)
            FROM (
                SELECT transaction_id
                FROM delta.gold_zone.obt_transaction_enriched
                GROUP BY transaction_id
                HAVING count(*) > 1
            ) duplicate_groups
        """,
        evaluator=evaluate_zero,
        field_paths=("transaction_id",),
    ),
    AssertionSpec(
        assertion_id="gold_feat_population_matches_dim_user",
        dataset_name="delta.gold_zone.feat_user_90d",
        description=(
            "Gold feat_user_90d has one current population row per dim_user row."
        ),
        expected="gold.dim_user row count = gold.feat_user_90d row count",
        contract_path="data_platform/contracts/gold_feat_user_90d.yml",
        validator_rule="dim_user_count_equals_feat_user_90d_count",
        cost="LOW",
        query="""
            SELECT
                (SELECT count(*) FROM delta.gold_zone.dim_user),
                (SELECT count(*) FROM delta.gold_zone.feat_user_90d)
        """,
        evaluator=evaluate_equal_counts,
    ),
    AssertionSpec(
        assertion_id="gold_feat_failed_rate_between_zero_and_one",
        dataset_name="delta.gold_zone.feat_user_90d",
        description=(
            "Every f_user_failed_transaction_rate_90d value is non-null and "
            "between 0 and 1 inclusive."
        ),
        expected="invalid or null failed-rate row count = 0; values in [0, 1]",
        contract_path="data_platform/contracts/gold_feat_user_90d.yml",
        validator_rule="feat_user_90d.failed_transaction_rate_between_0_and_1",
        cost="LOW",
        query="""
            SELECT
                count_if(
                    f_user_failed_transaction_rate_90d IS NULL
                    OR f_user_failed_transaction_rate_90d < 0
                    OR f_user_failed_transaction_rate_90d > 1
                ),
                min(f_user_failed_transaction_rate_90d),
                max(f_user_failed_transaction_rate_90d)
            FROM delta.gold_zone.feat_user_90d
        """,
        evaluator=evaluate_rate_range,
        field_paths=("f_user_failed_transaction_rate_90d",),
    ),
)


FEATURE_ASSERTIONS = (
    AssertionSpec(
        assertion_id="gold_feat_user_behavior_contract",
        dataset_name="delta.gold_zone.feat_user_behavior",
        description="User feature snapshots have a unique grain, required keys, and valid ranges.",
        expected="duplicate or invalid row count = 0",
        contract_path="data_platform/contracts/gold_feat_user_behavior.yml",
        validator_rule="feat_user_behavior.persisted_contract",
        cost="HIGH",
        query="""
            SELECT sum(invalid_count) FROM (
                SELECT count(*) invalid_count FROM (
                    SELECT user_id, event_timestamp FROM delta.gold_zone.feat_user_behavior
                    GROUP BY user_id, event_timestamp HAVING count(*) > 1
                ) duplicates
                UNION ALL
                SELECT count_if(user_id IS NULL OR event_timestamp IS NULL
                    OR user_tx_count_5m < 0 OR user_tx_count_5m > user_tx_count_1h
                    OR user_tx_count_1h > user_tx_count_24h
                    OR (user_failed_rate_24h IS NOT NULL
                        AND (user_failed_rate_24h < 0 OR user_failed_rate_24h > 1)))
                FROM delta.gold_zone.feat_user_behavior
            ) checks
        """,
        evaluator=evaluate_zero,
        field_paths=("user_id", "event_timestamp"),
    ),
    AssertionSpec(
        assertion_id="gold_feat_account_behavior_contract",
        dataset_name="delta.gold_zone.feat_account_behavior",
        description="Account feature snapshots have a unique grain, required keys, and valid ranges.",
        expected="duplicate or invalid row count = 0",
        contract_path="data_platform/contracts/gold_feat_account_behavior.yml",
        validator_rule="feat_account_behavior.persisted_contract",
        cost="HIGH",
        query="""
            SELECT sum(invalid_count) FROM (
                SELECT count(*) invalid_count FROM (
                    SELECT account_id, event_timestamp FROM delta.gold_zone.feat_account_behavior
                    GROUP BY account_id, event_timestamp HAVING count(*) > 1
                ) duplicates
                UNION ALL
                SELECT count_if(account_id IS NULL OR event_timestamp IS NULL
                    OR account_tx_count_5m < 0 OR account_tx_count_5m > account_tx_count_1h
                    OR account_tx_count_1h > account_tx_count_24h
                    OR (account_failed_rate_24h IS NOT NULL
                        AND (account_failed_rate_24h < 0 OR account_failed_rate_24h > 1)))
                FROM delta.gold_zone.feat_account_behavior
            ) checks
        """,
        evaluator=evaluate_zero,
        field_paths=("account_id", "event_timestamp"),
    ),
    AssertionSpec(
        assertion_id="gold_feat_device_behavior_contract",
        dataset_name="delta.gold_zone.feat_device_behavior",
        description="Device feature snapshots have a unique grain, required keys, and valid ranges.",
        expected="duplicate or invalid row count = 0",
        contract_path="data_platform/contracts/gold_feat_device_behavior.yml",
        validator_rule="feat_device_behavior.persisted_contract",
        cost="HIGH",
        query="""
            SELECT sum(invalid_count) FROM (
                SELECT count(*) invalid_count FROM (
                    SELECT device_id, event_timestamp FROM delta.gold_zone.feat_device_behavior
                    GROUP BY device_id, event_timestamp HAVING count(*) > 1
                ) duplicates
                UNION ALL
                SELECT count_if(device_id IS NULL OR event_timestamp IS NULL
                    OR device_age_seconds < 0 OR device_tx_count_1h < 0
                    OR device_tx_count_1h > device_tx_count_24h
                    OR (device_failed_rate_24h IS NOT NULL
                        AND (device_failed_rate_24h < 0 OR device_failed_rate_24h > 1)))
                FROM delta.gold_zone.feat_device_behavior
            ) checks
        """,
        evaluator=evaluate_zero,
        field_paths=("device_id", "event_timestamp"),
    ),
    AssertionSpec(
        assertion_id="gold_feat_merchant_behavior_contract",
        dataset_name="delta.gold_zone.feat_merchant_behavior",
        description="Merchant feature snapshots have required keys and valid feature ranges.",
        expected="invalid required-field or range row count = 0",
        contract_path="data_platform/contracts/gold_feat_merchant_behavior.yml",
        validator_rule="feat_merchant_behavior.persisted_contract",
        cost="HIGH",
        query="""
            SELECT count_if(merchant_id IS NULL OR event_timestamp IS NULL
                OR merchant_tx_count_10m < 0
                OR merchant_tx_count_10m > merchant_tx_count_1h
                OR merchant_tx_count_1h > merchant_tx_count_24h
                OR merchant_unique_users_10m > merchant_unique_users_1h)
            FROM delta.gold_zone.feat_merchant_behavior
        """,
        evaluator=evaluate_zero,
        field_paths=("merchant_id", "event_timestamp"),
    ),
)


FRAUD_ASSERTIONS = (
    AssertionSpec(
        assertion_id="silver_fraud_labels_unique_transaction_id",
        dataset_name="delta.silver_zone.fraud_labels",
        description="Silver fraud labels contain one row per transaction_id.",
        expected="duplicate transaction_id group count = 0",
        contract_path="data_platform/contracts/silver_fraud_labels.yml",
        validator_rule="fraud_labels.duplicate_key",
        cost="MEDIUM",
        query="""
            SELECT count(*)
            FROM (
                SELECT transaction_id
                FROM delta.silver_zone.fraud_labels
                GROUP BY transaction_id
                HAVING count(*) > 1
            ) duplicate_groups
        """,
        evaluator=evaluate_zero,
        field_paths=("transaction_id",),
    ),
    AssertionSpec(
        assertion_id="silver_fraud_labels_required_domain_semantics",
        dataset_name="delta.silver_zone.fraud_labels",
        description=(
            "Silver fraud labels have required keys and binary labels, with "
            "fraud_type present only for controlled positive-label values."
        ),
        expected="invalid required/domain/conditional row count = 0",
        contract_path="data_platform/contracts/silver_fraud_labels.yml",
        validator_rule="fraud_labels.required_domain_semantics",
        cost="LOW",
        query="""
            SELECT count_if(
                transaction_id IS NULL
                OR label IS NULL
                OR label NOT IN (0, 1)
                OR (label = 0 AND fraud_type IS NOT NULL)
                OR (label = 1 AND fraud_type IS NULL)
                OR (
                    label = 1
                    AND fraud_type NOT IN (
                        'velocity',
                        'amount_anomaly',
                        'account_takeover',
                        'merchant_burst'
                    )
                )
            )
            FROM delta.silver_zone.fraud_labels
        """,
        evaluator=evaluate_zero,
        field_paths=("transaction_id", "label", "fraud_type"),
    ),
    AssertionSpec(
        assertion_id="silver_fraud_labels_exact_transaction_coverage",
        dataset_name="delta.silver_zone.fraud_labels",
        description=(
            "Silver fraud labels and Silver transactions have exactly the "
            "same transaction_id population."
        ),
        expected=(
            "0 orphan labels, 0 missing labels, and equal label/transaction "
            "row counts"
        ),
        contract_path="data_platform/contracts/silver_fraud_labels.yml",
        validator_rule="fraud_labels.exact_transaction_coverage",
        cost="HIGH",
        query="""
            SELECT
                (
                    SELECT count(*)
                    FROM delta.silver_zone.fraud_labels labels
                    LEFT JOIN delta.silver_zone.transactions transactions
                        ON labels.transaction_id = transactions.transaction_id
                    WHERE transactions.transaction_id IS NULL
                ),
                (
                    SELECT count(*)
                    FROM delta.silver_zone.transactions transactions
                    LEFT JOIN delta.silver_zone.fraud_labels labels
                        ON transactions.transaction_id = labels.transaction_id
                    WHERE labels.transaction_id IS NULL
                ),
                (SELECT count(*) FROM delta.silver_zone.fraud_labels),
                (SELECT count(*) FROM delta.silver_zone.transactions)
        """,
        evaluator=evaluate_exact_coverage,
        field_paths=("transaction_id",),
    ),
)


def get_assertions(
    include_fraud_labels: bool = False,
    include_feature_tables: bool = False,
) -> tuple[AssertionSpec, ...]:
    assertions = ASSERTIONS
    if include_fraud_labels:
        assertions += FRAUD_ASSERTIONS
    if include_feature_tables:
        assertions += FEATURE_ASSERTIONS
    return assertions


@dataclass(frozen=True)
class AssertionResult:
    spec: AssertionSpec
    evaluation: Evaluation
    runtime_seconds: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate selected contract assertions through Trino and "
            "optionally publish definitions and run results to DataHub."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and print assertions without contacting DataHub.",
    )
    mode.add_argument(
        "--publish",
        action="store_true",
        help="Verify DataHub targets, then publish definitions and run results.",
    )
    mode.add_argument(
        "--self-test",
        action="store_true",
        help="Run small evaluator and deterministic-identity checks only.",
    )
    parser.add_argument(
        "--include-fraud-labels",
        action="store_true",
        help=(
            "Require and evaluate the three Silver fraud-label assertions. "
            "Use only after fraud tables exist in Trino/DataHub."
        ),
    )
    parser.add_argument(
        "--include-feature-tables",
        action="store_true",
        help="Evaluate four canonical Gold historical-feature assertions.",
    )
    parser.add_argument(
        "--datahub-gms-url",
        default=os.getenv("DATAHUB_GMS_URL", DEFAULT_DATAHUB_GMS_URL),
        help=(
            "DataHub GMS URL for --publish (default: DATAHUB_GMS_URL or "
            f"{DEFAULT_DATAHUB_GMS_URL})."
        ),
    )
    return parser.parse_args()


def run_self_test(assertions: Sequence[AssertionSpec] = ASSERTIONS) -> bool:
    assertion_ids = [spec.assertion_id for spec in assertions]
    assertion_urns = [spec.assertion_urn for spec in assertions]
    checks = (
        ("zero_pass", evaluate_zero((0,)).passed),
        ("zero_fail", not evaluate_zero((1,)).passed),
        ("equal_pass", evaluate_equal_counts((10, 10)).passed),
        ("equal_fail", not evaluate_equal_counts((9, 10)).passed),
        ("range_pass", evaluate_rate_range((0, 0.2, 0.9)).passed),
        ("range_fail", not evaluate_rate_range((1, -0.1, 0.9)).passed),
        (
            "coverage_pass",
            evaluate_exact_coverage((0, 0, 10, 10)).passed,
        ),
        (
            "coverage_fail",
            not evaluate_exact_coverage((0, 1, 10, 11)).passed,
        ),
        (
            "unique_assertion_ids",
            len(assertion_ids) == len(set(assertion_ids)),
        ),
        (
            "unique_assertion_urns",
            len(assertion_urns) == len(set(assertion_urns)),
        ),
        (
            "stable_assertion_urn",
            assertions[0].assertion_urn
            == make_assertion_urn(assertions[0].assertion_id),
        ),
    )

    all_passed = True
    for name, passed in checks:
        print(f"{'PASS' if passed else 'FAIL'} | self_test.{name}")
        all_passed = all_passed and passed

    return all_passed


def create_trino_connection():
    return connect(
        host=TRINO_HOST,
        port=TRINO_PORT,
        user=TRINO_USER,
        catalog=TRINO_CATALOG,
        http_scheme="http",
    )


def evaluate_assertions(
    assertions: Sequence[AssertionSpec] = ASSERTIONS,
) -> list[AssertionResult]:
    connection = create_trino_connection()
    cursor = connection.cursor()
    results: list[AssertionResult] = []

    try:
        cursor.execute("SELECT 1")
        if cursor.fetchone()[0] != 1:
            raise RuntimeError("Trino connection check returned an unexpected result.")

        for spec in assertions:
            started_at = time.perf_counter()
            cursor.execute(spec.query)
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"No result returned for {spec.assertion_id}.")

            evaluation = spec.evaluator(row)
            runtime_seconds = time.perf_counter() - started_at
            results.append(
                AssertionResult(
                    spec=spec,
                    evaluation=evaluation,
                    runtime_seconds=runtime_seconds,
                )
            )
    finally:
        cursor.close()
        connection.close()

    return results


def print_results(results: Sequence[AssertionResult]) -> None:
    for result in results:
        status = "PASS" if result.evaluation.passed else "FAIL"
        observed = json.dumps(
            result.evaluation.observed,
            sort_keys=True,
            default=str,
        )
        print(
            f"{status} | {result.spec.assertion_id} "
            f"| dataset={result.spec.dataset_name} "
            f"| observed={observed} "
            f"| expected={result.spec.expected} "
            f"| runtime={result.runtime_seconds:.3f}s"
        )


def create_datahub_client(gms_url: str) -> DataHubClient:
    token = os.getenv("DATAHUB_GMS_TOKEN")
    client = DataHubClient(server=gms_url, token=token)
    try:
        client.test_connection()
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{gms_url} did not return a DataHub GMS JSON configuration. "
            "Verify that the endpoint is serving DataHub rather than another "
            "local service."
        ) from exc
    return client


def verify_target_datasets(
    client: DataHubClient,
    assertions: Sequence[AssertionSpec] = ASSERTIONS,
) -> None:
    dataset_urns = sorted({spec.dataset_urn for spec in assertions})
    for dataset_urn in dataset_urns:
        client.entities.get(dataset_urn)
        print(f"PASS | datahub_dataset_exists | urn={dataset_urn}")


def publish_assertions(
    client: DataHubClient,
    results: Sequence[AssertionResult],
    gms_url: str,
) -> None:
    timestamp_millis = int(time.time() * 1000)
    emitter = DatahubRestEmitter(gms_server=gms_url)

    for result in results:
        spec = result.spec
        response = client.assertions.sync_custom_assertion(
            entity_urn=spec.dataset_urn,
            type=ASSERTION_TYPE,
            description=spec.description,
            platform_urn=ASSERTION_PLATFORM_URN,
            urn=spec.assertion_urn,
            field_paths=list(spec.field_paths) or None,
            logic=spec.query.strip(),
            native_type=spec.validator_rule,
            native_parameters=[
                {"key": "contract", "value": spec.contract_path},
                {"key": "cost", "value": spec.cost},
                {"key": "expected", "value": spec.expected},
            ],
        )
        published_urn = response.get("urn")
        if published_urn != spec.assertion_urn:
            raise RuntimeError(
                f"DataHub returned assertion URN {published_urn!r}; "
                f"expected {spec.assertion_urn!r}."
            )

        observed = json.dumps(
            result.evaluation.observed,
            sort_keys=True,
            default=str,
        )
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=spec.assertion_urn,
                aspect=AssertionRunEventClass(
                    timestampMillis=timestamp_millis,
                    runId=str(timestamp_millis),
                    asserteeUrn=spec.dataset_urn,
                    assertionUrn=spec.assertion_urn,
                    status=AssertionRunStatusClass.COMPLETE,
                    result=AssertionResultClass(
                        type=(
                            AssertionResultTypeClass.SUCCESS
                            if result.evaluation.passed
                            else AssertionResultTypeClass.FAILURE
                        ),
                        nativeResults={
                            "observed": observed,
                            "expected": spec.expected,
                            "validator_rule": spec.validator_rule,
                            "contract": spec.contract_path,
                        },
                    ),
                ),
            )
        )

        status = "SUCCESS" if result.evaluation.passed else "FAILURE"
        print(
            f"PUBLISHED | {spec.assertion_id} | urn={spec.assertion_urn} "
            f"| run_status={status} | timestamp_millis={timestamp_millis}"
        )


def main() -> int:
    args = parse_args()
    assertions = get_assertions(
        args.include_fraud_labels,
        args.include_feature_tables,
    )

    if args.self_test:
        return 0 if run_self_test(assertions) else 1

    try:
        client = None
        if args.publish:
            client = create_datahub_client(args.datahub_gms_url)
            verify_target_datasets(client, assertions)

        results = evaluate_assertions(assertions)
        print_results(results)

        if client is not None:
            publish_assertions(client, results, args.datahub_gms_url)

        pass_count = sum(result.evaluation.passed for result in results)
        fail_count = len(results) - pass_count
        print("=" * 60)
        print(f"Assertion summary: {pass_count} PASS | {fail_count} FAIL")

        return 0 if fail_count == 0 else 1
    except Exception as exc:
        print(f"ERROR | {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
