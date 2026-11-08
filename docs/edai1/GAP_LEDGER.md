# EDAI-1 gap ledger

**Companion to:** [RUBRIC_EVIDENCE_INDEX.md](RUBRIC_EVIDENCE_INDEX.md).

**Gap classes and priorities:** the same as in the [EDAI-2 ledger](../edai2/GAP_LEDGER.md).
- **P1:** no cloud mutation.
- **P2:** an owner-approved bounded run.
- **P3:** implementation.

[EDAI1_RUBRIC_COVERAGE.md](../../archive/legacy-docs/docs/edai1/EDAI1_RUBRIC_COVERAGE.md) §8 already lists low-cost actions. This ledger keeps them and adds the N13 findings.

| Row | Pts | Class | Existing evidence | Missing proof | Recommended action | Priority |
|---:|---:|---|---|---|---|---|
| 35 | 2 | IMPLEMENTATION_MISSING (submission tree) | `scd2.py`, `build_dim_merchant_scd2` and `tests/test_merchant_scd2.py` exist as **uncommitted** work in the main checkout. 4 local tests passed (2026-11-02). LOCAL FIXTURE images | The source is in the submitted tree; a real (non-fixture) SCD2 table | Owner reviews and commits the uncommitted SCD2 work; then a bounded DP2 run that writes `dim_merchant` with `valid_from_ts`/`valid_to_ts`/`is_current`, with a captured query | P1 (commit) → P2 (run) |
| 36 | 2 | IMPLEMENTATION_MISSING (submission tree) | `event_timestamp` on all `feat_` tables; `feat_user_90d.created_timestamp`. The `created` alias and builder changes are uncommitted | `created` in the submitted source; a materialized schema | Commit the feature-builder and contract changes; one bounded DP3 run; capture the schema | P1 → P2 |
| 34 | 2 | SCREENSHOT_MISSING | Per-zone inventory of 40 physical tables from the cloud Delta logs and Parquet footers in [ALL_ZONES_SCHEMA.md](schema/ALL_ZONES_SCHEMA.md); all four zones registered in a local read-only Trino and read back (TRINO_METADATA_VERIFIED, 2026-11-08); DataHub lineage images | DBeaver screenshots E01–E04 | Owner runs the [DBeaver runbook](../runbooks/edai1/SCHEMA_VISUALIZATION_DBEAVER.md) §B–F | P1 |
| 37 | 2 | SCREENSHOT_MISSING | [SCHEMA_DESIGN.md §7](schema/SCHEMA_DESIGN.md#7-dimensionfact-relationships): 13 edges with grain, cardinality and optionality, star/snowflake analysis; [GOLD_ERD.md](schema/generated/GOLD_ERD.md) lists the DBeaver virtual keys | DBeaver ER diagram E05/E06 | Owner runs the [DBeaver runbook](../runbooks/edai1/SCHEMA_VISUALIZATION_DBEAVER.md) §G | P1 |
| 38 | 2 | SCREENSHOT_MISSING | Naming table with the `raw_`/`stg_` equivalence in [SCHEMA_DESIGN.md §8](schema/SCHEMA_DESIGN.md#8-naming-conventions); catalog listing confirms `bronze_zone`/`silver_zone`/`gold_zone` and the Gold prefixes | DBeaver screenshot E07; assessor acceptance of the "similar" convention | Owner runs the [DBeaver runbook](../runbooks/edai1/SCHEMA_VISUALIZATION_DBEAVER.md) §H | P1 |
| 11 | 2 | SCREENSHOT_MISSING | DP1 Spark UI (3 images) plus N13 DP1 Airflow and Dataproc images | DP2/DP3 Spark UI | Recover Spark UI images from the Dataproc persistent history of the preserved DP2/DP3 batches, if still retained | P1 |
| 13 | 3 | DOCUMENTATION_MISSING | V1/V2 normalization implemented; source/unified schema images | A row-specific chain: problem → code → before/after → result | Consolidate one section in OPTIMIZATION_MATRIX.md using the existing images | P1 |
| 5 | 2 | RUNTIME_EVIDENCE_MISSING | Volume, schema and duplicate proof | Skew and cardinality ratios computed over the full 100 GB | A bounded profiling job (approx. distinct per column) on the canonical dataset | P2 |
| 12 | 3 | RUNTIME_EVIDENCE_MISSING | Real A/B; the optimized run was slower and is documented honestly | Skew diagnosis in the Spark UI (task-time skew) and a winning method | Bounded DP2 skew experiment with Spark UI capture | P2 |
| 14 | 3 | RUNTIME_EVIDENCE_MISSING | Silver 8/8 equivalent | Gold equivalence PASS | Validation-only recovery run of the Gold gate (the fix has 18 local tests) | P2 |
| 15 | 2 | RUNTIME_EVIDENCE_MISSING | DP1 and DP3 runs succeeded | DP2 complete success | Same as rows 14/25 | P2 |
| 25 | 2 | RUNTIME_EVIDENCE_MISSING | Physical checks passed | Final Gold gate PASS | Same as row 14 | P2 |
| 27 | 2 | RUNTIME_EVIDENCE_MISSING | Physical validation in the DP3 run; PIT 310/310 outside Airflow | PIT validation as an Airflow task, on the new config | Bounded DP3 run including the PIT task | P2 |
| 31 | 2 | RUNTIME_EVIDENCE_MISSING | Silver assertions PASS | A DP2 Gold assertion run event and its UI | Run the defined Gold assertion and capture the DataHub UI | P2 |
| 16–17 | 5 | RUNTIME_EVIDENCE_MISSING | Historical local Flink A/B (`22c0c7f`, 11 images), watermark UI | A run of the latest Block A source `0466ba4` with an authoritative late-arrival metric artifact | Bounded Redpanda/Flink scenario (late, duplicate, window) with Flink UI capture; keep the historical failed 500 EPS run visible | P2 |

## Closed in the N13 quality gate

| Row | Change |
|---|---|
| 19 | DOCUMENTATION gap closed: [FLINK_STREAMING.md](streaming/FLINK_STREAMING.md#31-window-processing-in-code) now shows the verbatim window and watermark code with an explanation |
| 18 | Accepted as complete (historical), in agreement with the N13 inventory |
| 38 (doc defect) | [ALL_ZONES_SCHEMA.md](schema/ALL_ZONES_SCHEMA.md) wrongly claimed a `stg_` prefix for Silver; corrected. The row stays PARTIAL |

## Process notes

- **No legacy files deleted.** All earlier EDAI-1 documents and evidence are kept unchanged, as the owner instructed, so they can be compared later. Superseded or overlapping reports (`EDAI1_RUBRIC_100_STATUS.md`, `RUBRIC_100_CROSS_WORKSTREAM.md`, `DATAHUB_*` variants) remain in place.
- **Local fixtures.** Screenshots labelled LOCAL FIXTURE or `LOCAL_AIRFLOW_PARSE_ONLY_NO_DAGRUN` must not be cited as runtime evidence.

## Corrections made during the documentation rewrite

| Report | Earlier text | Corrected from evidence |
|---|---|---|
| Flink | Maximum backpressured time "72 vs 80 ms/s" | 727 vs 80 ms/s, as shown in the comparison report screenshot |
| Schema Design | SCD2 builder and `created` columns described as present in the source | They are prototypes verified on local fixtures; the code is not in the submitted tree |
| DataHub | "Assertion modules not present at the canonical HEAD" | The modules are present in `data_platform/metadata/datahub/assertions/` |
| All-Zones Schema | Silver uses a `stg_` prefix | Bronze and Silver use zone-qualified names without prefixes |
