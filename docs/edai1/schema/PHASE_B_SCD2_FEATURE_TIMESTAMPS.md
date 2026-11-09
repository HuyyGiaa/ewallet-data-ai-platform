# Schema Design Phase B: SCD2 merchant dimension and feature timestamps

Status: **implemented and tested locally only.** No cloud run has materialized
`dim_merchant_scd2` or the `created` feature column. Rubric rows 35 and 36 are
not CLOUD_VERIFIED or EVIDENCE_COMPLETE. They need the owner's 5GB and 100GB runs
and owner-captured evidence.

## 1. What changed

| Area | Change |
|---|---|
| Dataset registry | `DatasetIdentity.merchant_history`. `rehearsal-5gb-v1` = true, `canonical-100gb-v1` = false. The flag is a registry entry, not business logic. |
| Generator | `offline/merchant_history.py` and `config/settings_merchant_history.yaml` (settings.yaml plus one block). Emits a tenth physical table, `merchant_history`. A private RNG leaves the nine legacy tables byte-identical. |
| Source manifest | `source_extensions.merchant_history` (provenance `synthetic`, generator module), present only when the table is. Legacy manifests are unchanged. |
| Bronze | `source_to_bronze.py` accepts the legacy nine tables or the declared extension. Approved datasets must match the registry. |
| Silver | `clean_merchant_history`, `--include-merchant-history`, `silver_tables(..., include_merchant_history)`. |
| Gold | `gold/scd2.py` (generic builder, `lookup_as_of`), `build_dim_merchant_scd2`, `gold_pipeline.py --include-merchant-scd2`, `core_gold_tables(include_merchant_scd2)`. `dim_merchant` is unchanged. |
| Features | `created` appended to all five `feat_` tables from one injectable UTC instant per run. `feat_user_90d.created_timestamp` kept. |
| Submitters | Silver and Gold submitters read the registry and pass the flags and the matching `--expected-table-count`. |
| Cloud summary | Inventory per dataset version. `created` is volatile when present. SCD2 interval rules are folded into `business_invariants` when the table exists. `created_populated` is informational. |
| Validators | `quality/scd2_quality.py`, `quality/feature_timestamp_quality.py`, `quality/results.py`. `validate_gold` is split into `core_gold_results` and `historical_feature_results`, with `--scope core-gold|features|all` and explicit run roots (required on gcp). `validate_silver --include-merchant-history`. |
| Feast | `feast_export_columns`: the export accepts the legacy schema or the schema plus `created` and always writes the legacy columns. |
| Contracts | New `silver_merchant_history.yml` and `gold_dim_merchant_scd2.yml`. The five `feat_` contracts declare `created`. |
| Diagrams | `schema_relationships.yml` `pending_observation`: the SCD2 key and edge are drawn only once a real `observed_schemas.json` contains the table. Committed diagrams are unchanged. |

## 2. Compatibility

- **`canonical-100gb-v1`**: no merchant history. Its Bronze/Silver/Gold inventories stay 8/8/11 (+4 DP3). Every guard, gate and immutable output is untouched.
- **Old outputs**: the Feast export, the cloud summary and `validate_gold --allow-legacy-feature-schema` still read outputs without `created`.
- **Feast/ML**: entity keys, `event_timestamp` (timestamp field) and the 31 predictor features are unchanged. `created` is not in `FEATURE_SPECS` and never reaches a Feast snapshot. No `created_timestamp_column` was set, so legacy snapshots stay readable. Online materialization and KServe read the Feast snapshot, so they are unaffected.
- **Feature values**: on a generated dataset (20,000 transactions), the outputs of the base-commit (`f871b33a`) builders and the new builders, minus `created`, are identical (`pandas.testing.assert_frame_equal`, exact) for all five tables.
- **Migration**: none for existing data. To use SCD2, a dataset must be generated with `settings_merchant_history.yaml` and registered with `merchant_history=True`.
- **Not changed** (follow-ups): the DataHub lineage lists (`create_lineage.py`, `cloud_discovery.py`), local Trino registration (`GOLD_TABLES`), `validate_canonical_source_gate.py` (canonical only), and the Airflow DAGs (the DP1–DP3 orchestration refactor is a separate task).

## 3. Local tests

See the handoff report for exact commands and counts. Spark tests use the
`fintech` conda env (PySpark 4.1.1, delta-spark 4.3.1) with
`PYSPARK_PYTHON=$HOME/miniconda3/envs/fintech/bin/python`. Generator tests also run in
the locked `data_platform/generation` uv project, and the Feast export test in the
locked `ai_platform/features/feast` project.

```bash
# Phase B unit, negative and contract tests
python -m pytest -q tests/unit/test_merchant_history_source.py tests/unit/test_merchant_scd2.py \
  tests/unit/test_feature_created_timestamps.py tests/unit/test_phase_b_contracts.py
# Local Spark + Delta pipeline (about 12 minutes)
EWALLET_RUN_SPARK_INTEGRATION=1 python -m pytest -q tests/integration/test_phase_b_pipeline_delta.py
# Feast export with created (Feast uv project)
uv run --locked --project ai_platform/features/feast python -m unittest tests.unit.test_feast_export_created
```

## 4. 5GB readiness

| Item | Locally verified | Still to check in the real 5GB run |
|---|---|---|
| Generation with history | 9 legacy tables identical; history deterministic, latest = merchants | Calibrate with `cloud-rehearsal-5gb.yaml` **and** `--config data_platform/generation/config/settings_merchant_history.yaml`; manifest shows `source_extensions` |
| Bronze | Manifest contract accepts/refuses correctly (unit) | Bronze evidence lists 9 tables incl. `merchant_history` |
| Silver | Cleaner + validator on Delta | Silver run writes 9 tables; `validate_silver --include-merchant-history` |
| Core Gold + SCD2 | Real pipeline on Delta; all core + SCD2 rules pass; legacy run writes 11 tables | Gold run writes 12 tables; `validate_gold --scope core-gold --include-merchant-scd2` on the run roots |
| DP3 features | Real pipeline on Delta; `created` = run clock; PIT recomputation passes | 4 tables carry `created`; `validate_gold --scope features` with a sampled PIT check |
| A/B summaries | Inventory per dataset, volatile `created` | Baseline and optimized summaries agree; `business_invariants` true |
| Feast | Export drops `created` (real Delta) | Do not repoint Feast to the rehearsal |

The rehearsal must use `settings_merchant_history.yaml`. Generating `rehearsal-5gb-v1` with `settings.yaml` would be refused at Bronze, because the registry expects the extension.

## 5. Owner runbook (read-only unless marked)

```bash
BUCKET=<rehearsal data bucket>        # never the canonical bucket
DATASET=rehearsal-5gb-v1
SILVER_RUN=<silver run id>; GOLD_RUN=<core gold run id>; FEATURE_RUN=<DP3 run id>
RUNS=gs://$BUCKET/lakehouse/runs
```

1. **Find the SCD2 Delta table**
   ```bash
   gcloud storage ls $RUNS/$GOLD_RUN/gold/
   gcloud storage ls $RUNS/$GOLD_RUN/gold/dim_merchant_scd2/_delta_log/
   ```
2. **SCD2 schema** (read-only, from the Delta log):
   ```bash
   gcloud storage cat $RUNS/$GOLD_RUN/gold/dim_merchant_scd2/_delta_log/00000000000000000000.json \
     | grep -o '"schemaString":"[^}]*' | head -1
   ```
   In Trino, after registering the table: `DESCRIBE <catalog>.<schema>.dim_merchant_scd2;`
   *(Registration with `CALL delta.system.register_table` writes catalog metadata. It is an owner action.)*
3. **Version distribution**
   ```sql
   SELECT versions, count(*) AS merchants
   FROM (SELECT merchant_id, count(*) AS versions FROM dim_merchant_scd2 GROUP BY merchant_id)
   GROUP BY versions ORDER BY versions;
   ```
4. **Active versions and overlap** (each must return 0)
   ```sql
   SELECT count(*) FROM (SELECT merchant_id FROM dim_merchant_scd2
     GROUP BY merchant_id HAVING sum(CASE WHEN is_current THEN 1 ELSE 0 END) <> 1);
   SELECT count(*) FROM dim_merchant_scd2 a JOIN dim_merchant_scd2 b
     ON a.merchant_id = b.merchant_id AND a.valid_from_ts < b.valid_from_ts
    AND (a.valid_to_ts IS NULL OR a.valid_to_ts > b.valid_from_ts);
   SELECT count(*) FROM dim_merchant_scd2 WHERE is_current <> (valid_to_ts IS NULL);
   ```
5. **As-of lookup** (exactly one row; at a boundary, the new version)
   ```sql
   SELECT merchant_id, merchant_name, category, valid_from_ts, valid_to_ts
   FROM dim_merchant_scd2
   WHERE merchant_id = '<id with >1 version>'
     AND valid_from_ts <= TIMESTAMP '<t> UTC'
     AND (valid_to_ts IS NULL OR TIMESTAMP '<t> UTC' < valid_to_ts);
   ```
6. **Feature schemas**: `DESCRIBE` each of `feat_user_behavior`, `feat_account_behavior`, `feat_device_behavior` and `feat_merchant_behavior` (DP3 run root), and `feat_user_90d` (core Gold run root).
7. **`event_timestamp` and `created`**
   ```sql
   SELECT count(*) AS rows, count(DISTINCT created) AS created_values,
          min(created), max(created), min(event_timestamp), max(event_timestamp),
          count_if(created IS NULL) AS null_created,
          count_if(created = event_timestamp) AS copied
   FROM feat_user_behavior;
   ```
   Expected: `created_values = 1`, `null_created = 0`, `copied = 0`, and the same `created` in all four DP3 tables.
8. **Row counts and timestamp validity**: compare each table's row count with the Gold/DP3 run evidence and the semantic summaries (`summarize_spark_output`). `event_timestamp` must fall inside the dataset's transaction range.
9. **Validator results** *(runs Spark: a Dataproc batch is a paid cloud job and needs owner approval; no submitter wrapper exists yet)*
   ```bash
   python -m data_platform.quality.validate_gold --environment gcp --runtime-config-file <profile> \
     --scope core-gold --include-merchant-scd2 \
     --silver-root $RUNS/$SILVER_RUN/silver --gold-root $RUNS/$GOLD_RUN/gold
   python -m data_platform.quality.validate_gold --environment gcp --runtime-config-file <profile> \
     --scope features --pit-sample-modulus 1000 \
     --silver-root $RUNS/$SILVER_RUN/silver --gold-root $RUNS/$GOLD_RUN/gold \
     --feature-gold-root $RUNS/$FEATURE_RUN/gold
   python -m data_platform.quality.validate_silver --environment gcp --runtime-config-file <profile> \
     --include-fraud-labels --include-merchant-history \
     --bronze-root $RUNS/<bronze run id>/bronze --silver-root $RUNS/$SILVER_RUN/silver
   ```
   Each prints a PASS/FAIL line per rule and exits non-zero on any failure. On gcp, `validate_gold` refuses non-run-scoped roots.
10. **Dataset version and run IDs**
    ```bash
    gcloud storage cat gs://$BUCKET/source/$DATASET/_control/dataset_manifest.json \
      | python -m json.tool | grep -A6 '"source_extensions"'
    gcloud storage ls $RUNS/$GOLD_RUN/evidence/ $RUNS/$FEATURE_RUN/evidence/
    ```
    The evidence and summaries must name `dataset_version = rehearsal-5gb-v1` and the expected run IDs.

On gcp both validators refuse to run without explicit run-scoped roots, so they
cannot fall back to a default or canonical location.
