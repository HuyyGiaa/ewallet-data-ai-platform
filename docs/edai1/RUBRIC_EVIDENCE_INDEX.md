# EDAI-1 rubric evidence index (rows 3–40, 100 points)

**Authority:** the original workbook `EDAI K11.xlsx`, sheet `edai-1 (50%)`.
The row table below uses short reviewer-facing labels; the exact requirement,
Proof/Deliverables wording and raw points are preserved by sheet and row in the
[93-row workbook extract](../rubric/edai_k11_workbook_extract.json).

This index is the N13 reviewer entry point. It reconciles three sources:
- [EDAI1_RUBRIC_COVERAGE.md](../../archive/legacy-docs/docs/edai1/EDAI1_RUBRIC_COVERAGE.md), the committed detailed matrix (kept unchanged);
- the N10-E2 93-row matrix;
- the actual content of the submission tree.

The status vocabulary and gap classes are the same as in the [EDAI-2 index](../edai2/RUBRIC_EVIDENCE_INDEX.md). Details are in [GAP_LEDGER.md](GAP_LEDGER.md). Screenshot classification and per-row screenshot coverage are in [SCREENSHOT_AUDIT.md](SCREENSHOT_AUDIT.md).

## DP1–DP3: rows 11–15 and 22–27 (read first)

DP1–DP3 are documented and evidenced **only** by the 5 GB campaign `ab-20261110225456` (`rehearsal-5gb-v1`, Git `aae22b08`, Baseline and Optimised), in [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md). Its evidence is under [`spark/rehearsal-5gb-ab-20261110225456/`](../evidence/edai1/spark/rehearsal-5gb-ab-20261110225456/README.md), with 21 genuine screenshots ([manifest](../evidence/edai1/spark/rehearsal-5gb-ab-20261110225456/screenshots/MANIFEST.md)).

The campaign established:
- 6 DagRuns and 16 Dataproc batches succeeded;
- 260 validation rules passed per profile;
- the two profiles are `SEMANTIC_EQUIVALENT` across all 34 tables (comparison batch `c1-20261110225456`).

The earlier 100 GB Spark/Airflow reports are archived outside the submission under `archive/legacy-docs/docs/edai1/` and are not used as evidence for these rows. The SCD2 and `created` code is committed (`356b2123`) and was validated on the 5 GB cloud outputs (rows 35–36).

The campaign does **not** change:
- the DataHub rows 28–33, because the run emitted no lineage or assertion events;
- the DBeaver rows 34, 37 and 38;
- the Flink rows;
- row 12, because no skew method was shown to improve on the Baseline.

## Other reconciliation notes

1. **Rows 9–10 and 16–19: Flink.**
   - The local Flink UI A/B at `22c0c7f` (11 images in [FLINK_STREAMING.md](streaming/FLINK_STREAMING.md)) is historical evidence.
   - The latest Block A source `0466ba4` was never executed.
   - Rows 9, 10 and 18 are accepted as *historical*. Row 19 is complete through the code section added in N13.
   - Rows 16–17 stay PARTIAL.
   - The disagreements with the N10-E2 matrix and the N13 inventory are recorded at the end of this file.

## Row index

| Row | Pts | Requirement | Status | Document | Key evidence | Gap |
|---:|---:|---|---|---|---|---|
| 3 | 2 | Docker & Docker Compose | PENDING_OWNER (5GB) | [DOCKER_OPTIMIZATION](docker/DOCKER_OPTIMIZATION.md) §6 | Hardened one-shot Compose service running the optimized campaign-verifier image on read-only 5GB evidence; ran locally with exit 0 (LOCAL_RUNTIME_VERIFIED) | OWNER_SCREENSHOT_PENDING (DK05) |
| 4 | 3 | Optimize Dockerfile (multi-stage) | PENDING_OWNER (5GB) | [DOCKER_OPTIMIZATION](docker/DOCKER_OPTIMIZATION.md) §3–5 | Baseline vs optimized Dockerfiles plus base-image × packaging control matrix; identical 45-check report fingerprint across all four variants (LOCAL_RUNTIME_VERIFIED). The October 88.44 % result is historical | OWNER_SCREENSHOT_PENDING (DK01–DK04); official sizes pending |
| 5 | 2 | Simulate skew (≥ 100 GB) | PARTIAL | [DATA_GENERATION](data_generator/DATA_GENERATION_AND_SIMULATION.md) | volume, schema and duplicate proof | RUNTIME_EVIDENCE_MISSING (full-column skew/cardinality scan of 100 GB) |
| 6 | 2 | Schema evolution | COMPLETE (historical) | same | V1/V2 fingerprints | — |
| 7 | 2 | Another offline problem (duplicates) | COMPLETE (historical) | same | 369.6 M logical / 376.99 M physical rows | — |
| 8 | 2 | Store generated data for Bronze | COMPLETE (historical) | same | immutable GCS shards + COMPLETE manifest | — |
| 9 | 2 | Late arrivals | COMPLETE (historical) | same, [FLINK_STREAMING](streaming/FLINK_STREAMING.md) | 528 on-time / 72 too-late of 600 | Latest source not run (note 3) |
| 10 | 2 | Another streaming problem (duplicates) | COMPLETE (historical) | same | 12,000 + 180 duplicates → 12,000 unique | Latest source not run (note 3) |
| 11 | 2 | Spark baseline | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §A.4, §B–D, §F | Baseline/Optimised profile per stage; Spark UI environment, SQL plans and stage metrics for DP1, Silver, Gold and DP3; event-log metrics; Airflow integration | Assessor acceptance; Optimised was not faster in one trial |
| 12 | 3 | Handle skew with explanation | PARTIAL | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §C.3, §F.4 | Skew mechanisms (AQE, broadcast threshold); Spark UI diagnosis of write-layout skew (Gold stage 173: median 4.41 s, max 27.25 s, both profiles) | RUNTIME_EVIDENCE_MISSING (no method measured to improve skew over the Baseline) |
| 13 | 3 | Handle schema evolution with explanation | PARTIAL | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §B.2 | V1/V2 union code; Bronze `channel_schema` and Silver `channel_not_null`/`valid_channel` PASS; Silver plan shows the `UNKNOWN` channel mapping | RUNTIME_EVIDENCE_MISSING (V1/V2 row split not measured on the 5GB run) |
| 14 | 3 | Handle other offline problem | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §C.2 | Duplicate deliveries: `row_number()` plan in Spark UI; Bronze 18,972,000 → Silver 18,600,000; `duplicate_key` = 0 (both profiles) | — |
| 15 | 2 | Spark integrated in pipelines | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §E | Every Spark transformation and validator is a DAG task; 6 DagRuns Success | — |
| 16 | 2 | Flink baseline | PARTIAL | [FLINK_STREAMING](streaming/FLINK_STREAMING.md) | historical A/B, 9 UI images | RUNTIME_EVIDENCE_MISSING (`0466ba4`) |
| 17 | 3 | Late arrival handling | PARTIAL | same | watermarks, allowed lateness | RUNTIME_EVIDENCE_MISSING |
| 18 | 3 | Other streaming problem | COMPLETE (historical) | [FLINK_STREAMING](streaming/FLINK_STREAMING.md) | dedup 12,180 → 12,000 at `22c0c7f`; backpressure UI baseline vs optimized | Latest source not run; failed 500 EPS run stays visible (note 3) |
| 19 | 2 | Window processing | COMPLETE | [FLINK_STREAMING §Row 19](streaming/FLINK_STREAMING.md#31-window-processing-in-code) | verbatim watermark + tumbling-window + allowed-lateness code with explanation (added in N13) | — (the deliverable is code + explanation) |
| 20 | 3 | Lakehouse optimization | PENDING_OWNER (5GB) | [STORAGE_OPTIMIZATION](storage/STORAGE_OPTIMIZATION.md) Part A | 5GB layout METADATA_VERIFIED (one ~24 MiB file per daily partition); isolated three-arm compaction/Z-order/pruning job verified on Spark 3.5.3 + Delta 3.2.0 locally | OWNER_CLOUD_EXECUTION_REQUIRED (one Dataproc batch); ST01–ST09 |
| 21 | 3 | Warehouse optimization (indexing) | PENDING_OWNER (5GB) | [STORAGE_OPTIMIZATION](storage/STORAGE_OPTIMIZATION.md) Part B | PostgreSQL 16 index benchmark on 3,816,574 real Gold rows (31 partitions); before/after, interleaved, write-cost and equivalence experiments ran locally (LOCAL_RUNTIME_VERIFIED) | OWNER_SCREENSHOT_PENDING (WH01–WH06, DBeaver) |
| 22 | 2 | DP1 ingest | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §B.4, §E | `bronze.wait_quota` → `submit_transformation` Success in both DagRuns; Airflow screenshots 01–02 | — |
| 23 | 2 | DP1 validate | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §B.3 | Source manifest check, `physical_validation`, `semantic_validation` (54 rules) PASS | — |
| 24 | 2 | DP2 ingest | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §C.6, §E | `silver` and `gold` transformations Success in both DagRuns; screenshots 03–04 | — |
| 25 | 2 | DP2 validate | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §C.5 | Silver (60 rules) and Gold (93 rules, incl. SCD2) PASS in both DagRuns | — |
| 26 | 2 | DP3 ingest | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §D.4, §E | `features` transformation Success in both DagRuns; screenshots 05–06 | — |
| 27 | 2 | DP3 validate | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §D.3 | `features.semantic_validation` (53 rules) incl. point-in-time recomputation, 0 mismatches | — |
| 28 | 2 | DP1 lineage | COMPLETE (historical) | [DATAHUB_GOVERNANCE](datahub/DATAHUB_GOVERNANCE.md) | DataFlow/DataJob → 8 Bronze datasets | — |
| 29 | 2 | DP1 validation | COMPLETE (historical) | same | `mismatch_count=0`, Passed | — |
| 30 | 2 | DP2 lineage | COMPLETE (historical) | same | Bronze → Silver → Gold, 3 images | — |
| 31 | 2 | DP2 validation | PARTIAL | same | Silver checks PASS | RUNTIME_EVIDENCE_MISSING (Gold assertion NOT_EXECUTED) |
| 32 | 2 | DP3 lineage | COMPLETE (historical) | same | Silver/Gold → 4 feature datasets | — |
| 33 | 2 | DP3 validation | COMPLETE (historical) | same | assertion run events | — |
| 34 | 2 | Visualize tables on all zones | PARTIAL | [ALL_ZONES_SCHEMA](schema/ALL_ZONES_SCHEMA.md) (zone visuals added in N13), [SCHEMA_DESIGN](schema/SCHEMA_DESIGN.md) | DataHub lineage per zone (HISTORICAL_UI ×4); 40-table inventory read back through Trino from the cloud runs (TRINO_METADATA_VERIFIED) | OWNER_SCREENSHOT_PENDING (DBeaver E01–E04) |
| 35 | 2 | Dim with SCD2 | COMPLETE (5GB) | [SCHEMA_DESIGN](schema/SCHEMA_DESIGN.md), [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §C.5 | `scd2.py` committed (`356b2123`); cloud `dim_merchant_scd2` (78,294 versions, `valid_from_ts`/`valid_to_ts`/`is_current`) passed 13 SCD2 rules in both profiles | Schema documentation still shows local-fixture images (deferred) |
| 36 | 2 | `feat_` tables with `event_timestamp` + `created` | COMPLETE (5GB) | [SCHEMA_DESIGN](schema/SCHEMA_DESIGN.md), [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §D.3 | All 4 DP3 tables and `feat_user_90d`: both columns typed `timestamp`, not null, `created` equals the recorded materialisation clock | — |
| 37 | 2 | Dim/fact relationships (DBeaver-like export) | PARTIAL | [SCHEMA_DESIGN](schema/SCHEMA_DESIGN.md), [GOLD_ERD](schema/generated/GOLD_ERD.md) | 13 logical edges with cardinality; DBeaver virtual-key specification | OWNER_SCREENSHOT_PENDING (DBeaver ER diagram E05/E06) |
| 38 | 2 | Naming convention | PARTIAL | same | zone-qualified names; explicit `raw_`/`stg_` equivalence (§8); catalog listing | OWNER_SCREENSHOT_PENDING (E07); assessor acceptance of "similar" |
| 39 | 10 | Novel idea 1 | COMPLETE | [01_DETERMINISTIC_SHARDING](novel_ideas/01_DETERMINISTIC_SHARDING.md) | runtime + business E2E | Assessor acceptance |
| 40 | 10 | Novel idea 2 | COMPLETE | [02_PIT_LEAKAGE_PREVENTION](novel_ideas/02_PIT_LEAKAGE_PREVENTION.md) | runtime + business E2E | Assessor acceptance |

## Summary

These are evidence-coverage buckets, not a predicted grade.

| Bucket | Rows | Points |
|---|---|---:|
| Complete deliverables (historical or 5GB campaign) | 6–11, 14, 15, 18, 19, 22–30, 32, 33, 35, 36, 39, 40 (25 rows) | 68 |
| 5GB rerun prepared; owner run or screenshots pending | 3, 4, 20, 21 (4 rows) | 11 |
| Missing only a screenshot or documentation | 34, 37, 38 (3 rows) | 6 |
| Missing runtime evidence | 5, 12, 13, 16, 17, 31 (6 rows) | 15 |
| Missing implementation (in the submission tree) | none | 0 |
| **Total** | 38 rows | **100** |

## Reconciliation with the external N13 inventory (quality gate, 2026-11-04)

**Inventory:** the owner's N13 evidence inventory (`FINAL_EVIDENCE_INVENTORY.md`, `FINAL_RUBRIC_STATUS.md`; external, 2026-11-04) reports EDAI-1 secured coverage 73/100. This index does not adopt it blindly. The two agree on rows 6–8, 15–17, 25, 27–30, 32, 33, 35, 36, 39 and 40.

| Row | N13 inventory | This index | Decision and evidence |
|---:|---|---|---|
| 5 | PASS | PARTIAL | [DATA_GENERATION](data_generator/DATA_GENERATION_AND_SIMULATION.md) itself states that the 100 GB skew/cardinality scan is `NOT_EXECUTED`. Only a 500k local profile exists |
| 9, 10 | PARTIAL | COMPLETE (historical) | The inventory's reason (dirty Block A source, no Redpanda runtime) concerns the newer Block A. The rows ask for documented late/duplicate characteristics, which the legacy generator evidence provides (12 % late, 528/72 of 600; 12,180 → 12,000) at an identified source |
| 11, 12, 13, 14 | HISTORICAL_PASS | 11, 14 COMPLETE (5GB); 12, 13 PARTIAL | Superseded by the 5GB campaign: Spark UI now exists for every pipeline and both profiles (rows 11, 14). Row 12 lacks a measured skew improvement and row 13 a measured V1/V2 split. See [SCREENSHOT_AUDIT](SCREENSHOT_AUDIT.md) |
| 18 | HISTORICAL_PASS | COMPLETE (historical) | Agreed (changed in this pass) |
| 19 | HISTORICAL_PASS | COMPLETE | Agreed after the code-and-explanation section was added in N13. Before that, the document had **no code snippet**, which is the literal deliverable |
| 22, 23, 24, 26 | PARTIAL ("authoritative DagRun closure absent") | COMPLETE (5GB) | Superseded: six authoritative DagRuns of the 5GB campaign, each with Airflow screenshots and metadata durations |
| 31 | HISTORICAL_PASS | PARTIAL | No DP2 assertion UI. The DP2 Gold assertion was `NOT_EXECUTED` ([EDAI1_RUBRIC_COVERAGE](../../archive/legacy-docs/docs/edai1/EDAI1_RUBRIC_COVERAGE.md)) |
| 34 | HISTORICAL_PASS | PARTIAL | DataHub lineage shows tables across zones (now embedded), but not the relationships within a zone |
| 37 | HISTORICAL_PASS | PARTIAL | DataHub lineage is not a dimension ↔ fact relationship export. No ERD exists |
| 38 | HISTORICAL_PASS | PARTIAL | Bronze/Silver have no `raw_`/`stg_` prefix ([SCHEMA_DESIGN](schema/SCHEMA_DESIGN.md) naming table). Acceptance as "similar" is the assessor's call. [ALL_ZONES_SCHEMA](schema/ALL_ZONES_SCHEMA.md) wrongly claimed a `stg_` prefix; corrected in N13 |

**Coverage:** 68/100 points in complete rows (2026-11-12: rows 3, 4, 20 and 21 moved from historical results to the 5GB rerun, pending the owner run). Row 13 moved from "missing documentation" to "missing runtime evidence", because its explanation now exists but the V1/V2 split on the 5GB run was not measured.
