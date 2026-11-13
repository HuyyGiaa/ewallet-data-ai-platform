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
| 3 | 2 | Docker & Docker Compose | COMPLETE (5GB) | [DOCKER_OPTIMIZATION](docker/DOCKER_OPTIMIZATION.md) §7 | Hardened one-shot Compose service (read-only rootfs and inputs, network none, cap_drop ALL, UID 65532, limits): config valid, run exit 0, PASS, no residual containers; owner screenshots DK05a/b ([manifest](../evidence/edai1/docker/5gb/MANIFEST.md)) | — |
| 4 | 3 | Optimize Dockerfile (multi-stage) | COMPLETE (5GB) | [DOCKER_OPTIMIZATION](docker/DOCKER_OPTIMIZATION.md) §4–6 | Owner run `20261112t142522`: 396,924,824 → 44,187,549 content bytes (−88.87 %), 95.76 % from the slim base and 4.24 % from multi-stage packaging (2×2 control matrix); identical 45-check fingerprint in all four variants; DK01–DK04 | No run-time gain (start-up dominated); stated in the report |
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
| 20 | 3 | Lakehouse optimization | COMPLETE (5GB) | [STORAGE_OPTIMIZATION](storage/STORAGE_OPTIMIZATION.md) Part A | Dataproc batch `storage-5gb-20261112t142522-storage` PASS: compaction a true no-op (NOT_NEEDED); Z-order rewrote 7.9 GB with no change in files or bytes read (DO_NOT_ADOPT, bounded by an A/A arm); partition pruning 7 of 151 files, 3.0–3.3× faster; all arms equivalent; ST01–ST09 in Trino/DBeaver and the Spark UI; ST09 rerun with UTC-pinned SQL: identical results on all three sides, 7 / 7 / 151 splits | Single Dataproc batch; Trino timings are single runs |
| 21 | 3 | Warehouse optimization (indexing) | COMPLETE (5GB) | [STORAGE_OPTIMIZATION](storage/STORAGE_OPTIMIZATION.md) Part B | PostgreSQL 16.15 on 3,816,574 real Gold rows: covering index, Parallel Seq Scan 174.073 ms → Index Only Scan 0.108 ms median (interleaved −99.945 %); WAL +17 %; equivalence PASS; WH01–WH06 in DBeaver | 31-day sample (20 % of rows); insert-time verdict INCONCLUSIVE |
| 22 | 2 | DP1 ingest | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §B.4, §E | `bronze.wait_quota` → `submit_transformation` Success in both DagRuns; Airflow screenshots 01–02 | — |
| 23 | 2 | DP1 validate | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §B.3 | Source manifest check, `physical_validation`, `semantic_validation` (54 rules) PASS | — |
| 24 | 2 | DP2 ingest | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §C.6, §E | `silver` and `gold` transformations Success in both DagRuns; screenshots 03–04 | — |
| 25 | 2 | DP2 validate | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §C.5 | Silver (60 rules) and Gold (93 rules, incl. SCD2) PASS in both DagRuns | — |
| 26 | 2 | DP3 ingest | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §D.4, §E | `features` transformation Success in both DagRuns; screenshots 05–06 | — |
| 27 | 2 | DP3 validate | COMPLETE (5GB) | [BATCH_PROCESSING_AND_ORCHESTRATION](BATCH_PROCESSING_AND_ORCHESTRATION.md) §D.3 | `features.semantic_validation` (53 rules) incl. point-in-time recomputation, 0 mismatches | — |
| 28 | 2 | DP1 lineage | PENDING_OWNER (5GB) | [DATAHUB_GOVERNANCE](datahub/DATAHUB_GOVERNANCE.md) §3 | Lineage plan from the verified campaign: 10 Source → `bronze.submit_transformation` → 9 Bronze (SOURCE_IMPLEMENTED, OFFLINE_TESTED) | DATAHUB_PUBLICATION_PENDING, OWNER_SCREENSHOT_PENDING (DG01) |
| 29 | 2 | DP1 validation | PENDING_OWNER (5GB) | [DATAHUB_GOVERNANCE](datahub/DATAHUB_GOVERNANCE.md) §4 | 54 executed Bronze rules PASS (CLOUD_OUTPUT_VERIFIED), mapped to 9 datasets | DATAHUB_PUBLICATION_PENDING (DG02) |
| 30 | 2 | DP2 lineage | PENDING_OWNER (5GB) | [DATAHUB_GOVERNANCE](datahub/DATAHUB_GOVERNANCE.md) §3 | Bronze → Silver → core Gold; `dim_merchant_scd2` ← `silver.merchant_history` | DATAHUB_PUBLICATION_PENDING (DG03, DG04) |
| 31 | 2 | DP2 validation | PENDING_OWNER (5GB) | [DATAHUB_GOVERNANCE](datahub/DATAHUB_GOVERNANCE.md) §4 | Silver 60 + Gold 93 executed rules PASS, incl. 14 on `dim_merchant_scd2` (CLOUD_OUTPUT_VERIFIED). The former Gold `NOT_EXECUTED` gap now has real results | DATAHUB_PUBLICATION_PENDING (DG05, DG06) |
| 32 | 2 | DP3 lineage | PENDING_OWNER (5GB) | [DATAHUB_GOVERNANCE](datahub/DATAHUB_GOVERNANCE.md) §3 | `silver.transactions`, `silver.devices` → DP3 → 4 feature tables | DATAHUB_PUBLICATION_PENDING (DG07) |
| 33 | 2 | DP3 validation | PENDING_OWNER (5GB) | [DATAHUB_GOVERNANCE](datahub/DATAHUB_GOVERNANCE.md) §4 | 53 executed rules incl. point-in-time recomputation PASS (56 outcomes) | DATAHUB_PUBLICATION_PENDING (DG08) |
| 34 | 2 | Visualize tables on all zones | PENDING_OWNER (5GB) | [SCHEMA_DESIGN](schema/SCHEMA_DESIGN.md) §3 | 34 Delta tables with rows/columns/keys from the campaign comparison; campaign-bound export and DBML renderer (OFFLINE_TESTED) | OWNER_METADATA_EXPORT_PENDING; OWNER_SCREENSHOT_PENDING (SC01–SC03) |
| 35 | 2 | Dim with SCD2 | PENDING_OWNER (5GB) | [SCHEMA_DESIGN](schema/SCHEMA_DESIGN.md) §5 | `dim_merchant_scd2`: 78,294 versions; 14 rules PASS in both profiles (CLOUD_OUTPUT_VERIFIED) | OWNER_SCREENSHOT_PENDING (SC05, SC06) |
| 36 | 2 | `feat_` tables with `event_timestamp` + `created` | PENDING_OWNER (5GB) | [SCHEMA_DESIGN](schema/SCHEMA_DESIGN.md) §6 | All 5 feature tables: typed, non-null, single shared `created` (CLOUD_OUTPUT_VERIFIED) | OWNER_SCREENSHOT_PENDING (SC07) |
| 37 | 2 | Dim/fact relationships | PENDING_OWNER (5GB) | [SCHEMA_DESIGN](schema/SCHEMA_DESIGN.md) §4 | 32 relationships each with a validator rule PASS in both profiles; dbdiagram.io DBML generator | OWNER_METADATA_EXPORT_PENDING; OWNER_SCREENSHOT_PENDING (SC02–SC04) |
| 38 | 2 | Naming convention | PENDING_OWNER (5GB) | [SCHEMA_DESIGN](schema/SCHEMA_DESIGN.md) §7 | zone-qualified business names + Gold role prefixes, tested | OWNER_SCREENSHOT_PENDING (SC08); assessor acceptance of "similar" |
| 39 | 10 | Novel idea 1 | COMPLETE | [01_DETERMINISTIC_SHARDING](novel_ideas/01_DETERMINISTIC_SHARDING.md) | runtime + business E2E | Assessor acceptance |
| 40 | 10 | Novel idea 2 | COMPLETE | [02_PIT_LEAKAGE_PREVENTION](novel_ideas/02_PIT_LEAKAGE_PREVENTION.md) | runtime + business E2E | Assessor acceptance |

## Summary

These are evidence-coverage buckets, not a predicted grade.

| Bucket | Rows | Points |
|---|---|---:|
| Complete deliverables (historical or 5GB campaign) | 3, 4, 6–11, 14, 15, 18–27, 39, 40 (22 rows) | 65 |
| 5GB code and cloud results ready; owner DataHub run, metadata export or screenshot pending | 28–38 (11 rows) | 22 |
| Missing runtime evidence | 5, 12, 13, 16, 17 (5 rows) | 13 |
| Missing implementation (in the submission tree) | none | 0 |
| **Total** | 38 rows | **100** |

## Reconciliation with the external N13 inventory (quality gate, 2026-11-04)

**Inventory:** the owner's N13 evidence inventory (`FINAL_EVIDENCE_INVENTORY.md`, `FINAL_RUBRIC_STATUS.md`; external, 2026-11-04) reports EDAI-1 secured coverage 73/100. This index does not adopt it blindly. The two agree on rows 3, 4, 6–8, 15–17, 20, 21, 25, 27, 39 and 40.

| Row | N13 inventory | This index | Decision and evidence |
|---:|---|---|---|
| 5 | PASS | PARTIAL | [DATA_GENERATION](data_generator/DATA_GENERATION_AND_SIMULATION.md) itself states that the 100 GB skew/cardinality scan is `NOT_EXECUTED`. Only a 500k local profile exists |
| 9, 10 | PARTIAL | COMPLETE (historical) | The inventory's reason (dirty Block A source, no Redpanda runtime) concerns the newer Block A. The rows ask for documented late/duplicate characteristics, which the legacy generator evidence provides (12 % late, 528/72 of 600; 12,180 → 12,000) at an identified source |
| 11, 12, 13, 14 | HISTORICAL_PASS | 11, 14 COMPLETE (5GB); 12, 13 PARTIAL | Superseded by the 5GB campaign: Spark UI now exists for every pipeline and both profiles (rows 11, 14). Row 12 lacks a measured skew improvement and row 13 a measured V1/V2 split. See [SCREENSHOT_AUDIT](SCREENSHOT_AUDIT.md) |
| 18 | HISTORICAL_PASS | COMPLETE (historical) | Agreed (changed in this pass) |
| 19 | HISTORICAL_PASS | COMPLETE | Agreed after the code-and-explanation section was added in N13. Before that, the document had **no code snippet**, which is the literal deliverable |
| 22, 23, 24, 26 | PARTIAL ("authoritative DagRun closure absent") | COMPLETE (5GB) | Superseded: six authoritative DagRuns of the 5GB campaign, each with Airflow screenshots and metadata durations |
| 28–33 | HISTORICAL_PASS | PENDING_OWNER (5GB) | The October 100GB DataHub publication is no longer the basis (owner directive 2026-11-11). The 5GB lineage and 263 assertion outcomes are implemented and tested offline; publication and DG01–DG08 are owner steps |

**Coverage:** 65/100 points in complete rows (2026-11-11: rows 28–38 moved to the 5GB basis and wait for the owner's DataHub run, metadata export and screenshots; rows 35–36 count once SC05–SC07 exist). Row 13 moved from "missing documentation" to "missing runtime evidence", because its explanation now exists but the V1/V2 split on the 5GB run was not measured.
