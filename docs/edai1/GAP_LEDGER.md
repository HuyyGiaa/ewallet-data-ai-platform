# EDAI-1 gap ledger

**Companion to:** [RUBRIC_EVIDENCE_INDEX.md](RUBRIC_EVIDENCE_INDEX.md).

**Gap classes and priorities:** the same as in the [EDAI-2 ledger](../edai2/GAP_LEDGER.md).
- **P1:** no cloud mutation.
- **P2:** an owner-approved bounded run.
- **P3:** implementation.

[EDAI1_RUBRIC_COVERAGE.md](../../archive/legacy-docs/docs/edai1/EDAI1_RUBRIC_COVERAGE.md) §8 already lists low-cost actions. This ledger keeps them and adds the N13 findings.

| Row | Pts | Class | Existing evidence | Missing proof | Recommended action | Priority |
|---:|---:|---|---|---|---|---|
| 13 | 3 | RUNTIME_EVIDENCE_MISSING | Problem, code and explanation in [BATCH_PROCESSING_AND_ORCHESTRATION.md](BATCH_PROCESSING_AND_ORCHESTRATION.md) §B.2; Bronze `channel_schema` and Silver `channel_not_null`/`valid_channel` PASS on the 5GB run | The V1/V2 row split measured on the 5GB Bronze/Silver outputs | A read-only count of null/non-null `channel` by version on the stored 5GB Bronze run | P1 |
| 5 | 2 | RUNTIME_EVIDENCE_MISSING | Volume, schema and duplicate proof | Skew and cardinality ratios computed over the full 100 GB | A bounded profiling job (approx. distinct per column) on the canonical dataset | P2 |
| 12 | 3 | RUNTIME_EVIDENCE_MISSING | Skew mechanisms and a Spark UI diagnosis of write-layout skew (date repartition, max/median 4.3–6.7× in both profiles), [BATCH_PROCESSING_AND_ORCHESTRATION.md](BATCH_PROCESSING_AND_ORCHESTRATION.md) §C.3, §F.4 | A method measured to improve the skewed stages over the Baseline | A controlled experiment on the date-repartition and merchant-window stages, with repeated trials | P2 |
| 16–17 | 5 | RUNTIME_EVIDENCE_MISSING | Historical local Flink A/B (`22c0c7f`, 11 images), watermark UI | A run of the latest Block A source `0466ba4` with an authoritative late-arrival metric artifact | Bounded Redpanda/Flink scenario (late, duplicate, window) with Flink UI capture; keep the historical failed 500 EPS run visible | P2 |

## Closed by the 5GB Docker and storage runs (2026-11-13)

| Row | Evidence that closed it | Residual note |
|---|---|---|
| 3, 4 | Owner run `20261112t142522`: 2×2 image matrix (−88.87 % content, 95.76 % base image / 4.24 % packaging), identical fingerprint, hardened Compose exit 0; DK01–DK05 ([report](docker/DOCKER_OPTIMIZATION.md)) | No run-time gain claimed |
| 20 | Dataproc batch `storage-5gb-20261112t142522-storage` PASS: compaction NOT_NEEDED, Z-order DO_NOT_ADOPT, partition pruning 7/151 files; ST01–ST09 ([report](storage/STORAGE_OPTIMIZATION.md) Part A) | ST09 recaptured on 2026-11-13 with UTC-pinned SQL; the first capture is kept as superseded provenance |
| 21 | PostgreSQL 16.15 index benchmark on 3,816,574 real Gold rows: 174.073 → 0.108 ms, WAL +17 %, equivalence PASS; WH01–WH06 ([report](storage/STORAGE_OPTIMIZATION.md) Part B) | 31-day sample |

## Closed by the 5GB rehearsal `ab-20261110225456` (2026-11-11)

| Row | Evidence that closed it |
|---|---|
| 11 | Spark UI for DP1, DP2 Silver/Gold and DP3 in both profiles, with the explanation in [BATCH_PROCESSING_AND_ORCHESTRATION.md](BATCH_PROCESSING_AND_ORCHESTRATION.md) §B–D |
| 14 | Deduplication shown in the Silver plan and validated in Airflow: 372,000 duplicates removed, 0 duplicate keys (§7.2) |
| 15, 25, 27 | 6 DagRuns Success, with Silver, Gold and feature validators (including point-in-time recomputation) as DAG tasks ([BATCH_PROCESSING_AND_ORCHESTRATION.md](BATCH_PROCESSING_AND_ORCHESTRATION.md) §E) |
| 35, 36 (cloud output) | SCD2 and `created` rules PASS on the 5GB cloud tables (§7.3–7.4). The rows stay open until SC05–SC07 exist (see table above) |

Also closed: the semantic comparison of the two profiles (`c1-20261110225456`): `SEMANTIC_EQUIVALENT` across all 34 tables.

Rows 22–24 and 26 are now evidenced by the 5GB DagRuns ([BATCH_PROCESSING_AND_ORCHESTRATION.md](BATCH_PROCESSING_AND_ORCHESTRATION.md) §E); the archived 100GB runs are no longer cited.

## Closed in the N13 quality gate

| Row | Change |
|---|---|
| 19 | DOCUMENTATION gap closed: [FLINK_STREAMING.md](streaming/FLINK_STREAMING.md#31-window-processing-in-code) now shows the verbatim window and watermark code with an explanation |
| 18 | Accepted as complete (historical), in agreement with the N13 inventory |
| 38 (doc defect) | The former all-zones report wrongly claimed a `stg_` prefix for Silver; corrected, and that report is now merged into [SCHEMA_DESIGN.md](schema/SCHEMA_DESIGN.md) |

## Changed on 2026-11-11 (Data Governance and Schema Design on the 5GB basis)

- Rows 28–33 no longer rest on the October 100GB DataHub publication; its images stay in `docs/evidence/edai1/datahub/` as history.
- Removed as obsolete (git history keeps them): the 100GB generated schema files, the DBeaver ERD workflow, `ALL_ZONES_SCHEMA.md`, `PHASE_B_SCD2_FEATURE_TIMESTAMPS.md`, `DATAHUB_ASSERTIONS_REPRODUCIBILITY.md`, and the 100GB-only `cloud_discovery.py`, `cloud_inventory_assertions.py`, `recover_validation_results.py`.
- `BATCH_PROCESSING_AND_ORCHESTRATION.md` said 51 task instances; the evidence holds 54 (7 + 12 + 8 per profile). Corrected.

## Process notes

- **No legacy files deleted (until 2026-11-11).** The 2026-11-11 directive authorized removing obsolete governance and schema files; see the section above. All earlier EDAI-1 documents and evidence are kept unchanged, as the owner instructed, so they can be compared later. Superseded or overlapping reports (`EDAI1_RUBRIC_100_STATUS.md`, `RUBRIC_100_CROSS_WORKSTREAM.md`, `DATAHUB_*` variants) remain in place.
- **Local fixtures.** Screenshots labelled LOCAL FIXTURE or `LOCAL_AIRFLOW_PARSE_ONLY_NO_DAGRUN` must not be cited as runtime evidence.

## Corrections made during the documentation rewrite

| Report | Earlier text | Corrected from evidence |
|---|---|---|
| Flink | Maximum backpressured time "72 vs 80 ms/s" | 727 vs 80 ms/s, as shown in the comparison report screenshot |
| Schema Design | SCD2 builder and `created` columns described as present in the source | They are prototypes verified on local fixtures; the code is not in the submitted tree |
| DataHub | "Assertion modules not present at the canonical HEAD" | The modules are present in `data_platform/metadata/datahub/assertions/` |
| All-Zones Schema (merged into Schema Design) | Silver uses a `stg_` prefix | Bronze and Silver use zone-qualified names without prefixes |

## Closed by the 5GB governance and schema work (2026-11-13)

| Row | Evidence that closed it | Residual note |
|---|---|---|
| 28–33 | DataHub publication of the verified 5GB campaign: 141 metadata records and 263 assertions (definitions and run events) read back `MATCH`, 0 skipped; DG01–DG08 ([report](datahub/DATAHUB_GOVERNANCE.md), [evidence](../evidence/edai1/datahub/rehearsal-5gb/MANIFEST.md)) | Lineage is published from the campaign record after the run, not emitted live; four leftover `submit_*_dataproc` DataJobs remain in the shared DataFlows |
| 34, 37 | Campaign-bound observed schema; Silver (12 references) and Gold + features (21) DBML imported at dbdiagram.io; 44 tables registered in an isolated read-only Trino, all locations verified; SC01–SC04 ([report](schema/SCHEMA_DESIGN.md), [evidence](../evidence/edai1/schema/rehearsal-5gb/MANIFEST.md)) | References are logical; 32 of 33 are checked by validator rules |
| 35, 36 | `dim_merchant_scd2` 78,294 versions (55,800 current, 22,494 historical, 14 rules PASS); feature `event_timestamp` and `created` typed `timestamp(3) with time zone`; SC05–SC07 | Synthetic merchant history |
| 38 | 44 tables classified, 0 unclassified; SC08 | Assessor acceptance of zone qualification instead of Bronze/Silver prefixes |
