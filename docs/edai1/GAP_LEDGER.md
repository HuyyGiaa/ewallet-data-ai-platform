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
| 13 | 3 | RUNTIME_EVIDENCE_MISSING | Problem, code and explanation in [BATCH_PROCESSING_AND_ORCHESTRATION.md](BATCH_PROCESSING_AND_ORCHESTRATION.md) §B.2; Bronze `channel_schema` and Silver `channel_not_null`/`valid_channel` PASS on the 5GB run | The V1/V2 row split measured on the 5GB Bronze/Silver outputs | A read-only count of null/non-null `channel` by version on the stored 5GB Bronze run | P1 |
| 5 | 2 | RUNTIME_EVIDENCE_MISSING | Volume, schema and duplicate proof | Skew and cardinality ratios computed over the full 100 GB | A bounded profiling job (approx. distinct per column) on the canonical dataset | P2 |
| 12 | 3 | RUNTIME_EVIDENCE_MISSING | Skew mechanisms and a Spark UI diagnosis of write-layout skew (date repartition, max/median 4.3–6.7× in both profiles), [BATCH_PROCESSING_AND_ORCHESTRATION.md](BATCH_PROCESSING_AND_ORCHESTRATION.md) §C.3, §F.4 | A method measured to improve the skewed stages over the Baseline | A controlled experiment on the date-repartition and merchant-window stages, with repeated trials | P2 |
| 31 | 2 | RUNTIME_EVIDENCE_MISSING | Silver assertions PASS | A DP2 Gold assertion run event and its UI | Run the defined Gold assertion and capture the DataHub UI | P2 |
| 16–17 | 5 | RUNTIME_EVIDENCE_MISSING | Historical local Flink A/B (`22c0c7f`, 11 images), watermark UI | A run of the latest Block A source `0466ba4` with an authoritative late-arrival metric artifact | Bounded Redpanda/Flink scenario (late, duplicate, window) with Flink UI capture; keep the historical failed 500 EPS run visible | P2 |

## Docker and storage rerun on the 5GB campaign (2026-11-12, branch `feature/edai1-docker-storage-5gb`)

| Row | Pts | Class | Prepared and verified | Missing proof | Action | Priority |
|---:|---:|---|---|---|---|---|
| 3, 4 | 5 | OWNER_SCREENSHOT_PENDING | Campaign-verifier images, 2×2 control matrix, hardened Compose; all ran locally with one shared report fingerprint | DK01–DK05 from the owner run | Stage 1 of [DOCKER_STORAGE_5GB_OWNER_EVIDENCE](../runbooks/edai1/DOCKER_STORAGE_5GB_OWNER_EVIDENCE.md) | P1 |
| 20 | 3 | OWNER_CLOUD_EXECUTION_REQUIRED | 5GB layout read from Delta logs; plan with data-derived predicates; job verified on Spark 3.5.3 + Delta 3.2.0 locally (clone safety, no-op detection, Z-order, pruning metrics) | One Dataproc batch; ST01–ST09 | Stages 3–4 of the runbook | P2 |
| 21 | 3 | OWNER_SCREENSHOT_PENDING | PostgreSQL benchmark on 3,816,574 real Gold rows ran locally end to end | WH01–WH06 in DBeaver | Stage 2 of the runbook | P1 |

## Closed by the 5GB rehearsal `ab-20261110225456` (2026-11-11)

| Row | Evidence that closed it |
|---|---|
| 11 | Spark UI for DP1, DP2 Silver/Gold and DP3 in both profiles, with the explanation in [BATCH_PROCESSING_AND_ORCHESTRATION.md](BATCH_PROCESSING_AND_ORCHESTRATION.md) §B–D |
| 14 | Deduplication shown in the Silver plan and validated in Airflow: 372,000 duplicates removed, 0 duplicate keys (§7.2) |
| 15, 25, 27 | 6 DagRuns Success, with Silver, Gold and feature validators (including point-in-time recomputation) as DAG tasks ([BATCH_PROCESSING_AND_ORCHESTRATION.md](BATCH_PROCESSING_AND_ORCHESTRATION.md) §E) |
| 35, 36 | SCD2 and `created` code committed (`356b2123`); cloud `dim_merchant_scd2` passed 13 SCD2 rules, and every feature table passed the `event_timestamp`/`created` rules (§7.3–7.4) |

Also closed: the semantic comparison of the two profiles (`c1-20261110225456`): `SEMANTIC_EQUIVALENT` across all 34 tables.

Rows 22–24 and 26 are now evidenced by the 5GB DagRuns ([BATCH_PROCESSING_AND_ORCHESTRATION.md](BATCH_PROCESSING_AND_ORCHESTRATION.md) §E); the archived 100GB runs are no longer cited.

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
