# EDAI-1 gap ledger

**Companion to:** [RUBRIC_EVIDENCE_INDEX.md](RUBRIC_EVIDENCE_INDEX.md).

**Gap classes and priorities:** the same as in the [EDAI-2 ledger](../edai2/GAP_LEDGER.md).
- **P1:** no cloud mutation.
- **P2:** an owner-approved bounded run.
- **P3:** implementation.

[EDAI1_RUBRIC_COVERAGE.md](../../archive/legacy-docs/docs/edai1/EDAI1_RUBRIC_COVERAGE.md) §8 already lists low-cost actions. This ledger keeps them and adds the N13 findings.

| Row | Pts | Class | Existing evidence | Missing proof | Recommended action | Priority |
|---:|---:|---|---|---|---|---|
| 34 | 2 | SCREENSHOT_MISSING | Per-zone inventory of 40 physical tables from the cloud Delta logs and Parquet footers in [ALL_ZONES_SCHEMA.md](schema/ALL_ZONES_SCHEMA.md); all four zones registered in a local read-only Trino and read back (TRINO_METADATA_VERIFIED, 2026-11-08); DataHub lineage images | DBeaver screenshots E01–E04 | Owner runs the [DBeaver runbook](../runbooks/edai1/SCHEMA_VISUALIZATION_DBEAVER.md) §B–F | P1 |
| 37 | 2 | SCREENSHOT_MISSING | [SCHEMA_DESIGN.md §7](schema/SCHEMA_DESIGN.md#7-dimensionfact-relationships): 13 edges with grain, cardinality and optionality, star/snowflake analysis; [GOLD_ERD.md](schema/generated/GOLD_ERD.md) lists the DBeaver virtual keys | DBeaver ER diagram E05/E06 | Owner runs the [DBeaver runbook](../runbooks/edai1/SCHEMA_VISUALIZATION_DBEAVER.md) §G | P1 |
| 38 | 2 | SCREENSHOT_MISSING | Naming table with the `raw_`/`stg_` equivalence in [SCHEMA_DESIGN.md §8](schema/SCHEMA_DESIGN.md#8-naming-conventions); catalog listing confirms `bronze_zone`/`silver_zone`/`gold_zone` and the Gold prefixes | DBeaver screenshot E07; assessor acceptance of the "similar" convention | Owner runs the [DBeaver runbook](../runbooks/edai1/SCHEMA_VISUALIZATION_DBEAVER.md) §H | P1 |
| 13 | 3 | DOCUMENTATION_MISSING | V1/V2 normalization implemented; source/unified schema images | A row-specific chain: problem → code → before/after → result | Consolidate one section in OPTIMIZATION_MATRIX.md using the existing images | P1 |
| 5 | 2 | RUNTIME_EVIDENCE_MISSING | Volume, schema and duplicate proof | Skew and cardinality ratios computed over the full 100 GB | A bounded profiling job (approx. distinct per column) on the canonical dataset | P2 |
| 12 | 3 | RUNTIME_EVIDENCE_MISSING | 100GB A/B (optimized slower); 5GB rehearsal Spark UI diagnosis of write-layout skew (date repartition, max/median 4.3–6.7× in both profiles; [report §6.5](spark/REHEARSAL_5GB_BENCHMARK.md)) | A method measured to improve the skewed stages over the baseline | At 100 GB, measure the date-repartition and merchant-window stages; test a sized repartition in repeated trials | P2 |
| 31 | 2 | RUNTIME_EVIDENCE_MISSING | Silver assertions PASS | A DP2 Gold assertion run event and its UI | Run the defined Gold assertion and capture the DataHub UI | P2 |
| 16–17 | 5 | RUNTIME_EVIDENCE_MISSING | Historical local Flink A/B (`22c0c7f`, 11 images), watermark UI | A run of the latest Block A source `0466ba4` with an authoritative late-arrival metric artifact | Bounded Redpanda/Flink scenario (late, duplicate, window) with Flink UI capture; keep the historical failed 500 EPS run visible | P2 |

## Closed by the 5GB rehearsal `ab-20261110225456` (2026-11-11)

| Row | Evidence that closed it |
|---|---|
| 11 | Spark UI for DP1, DP2 Silver/Gold and DP3 in both profiles, with the explanation in [REHEARSAL_5GB_BENCHMARK.md](spark/REHEARSAL_5GB_BENCHMARK.md) §4–6 |
| 14 | Deduplication shown in the Silver plan and validated in Airflow: 372,000 duplicates removed, 0 duplicate keys (§7.2) |
| 15, 25, 27 | 6 DagRuns Success, with Silver, Gold and feature validators (including point-in-time recomputation) as DAG tasks ([Airflow §10](airflow/AIRFLOW_DP1_DP2_DP3_EVIDENCE.md#10-three-dag-run-on-the-5gb-rehearsal-ab-20261110225456)) |
| 35, 36 | SCD2 and `created` code committed (`356b2123`); cloud `dim_merchant_scd2` passed 13 SCD2 rules, and every feature table passed the `event_timestamp`/`created` rules (§7.3–7.4) |

Still open: the semantic comparison of the two profiles (`c1-20261110225456`, PENDING). No rubric row depends on it.

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
