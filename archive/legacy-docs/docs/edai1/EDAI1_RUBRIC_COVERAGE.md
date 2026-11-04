# EDAI-1 Rubric Coverage and Evidence Matrix

## 1. Executive summary

This report maps every scored requirement in worksheet `edai-1 (50%)` of
`EDAI K11.xlsx` to the implementation, tests, historical runtime artifacts and
screenshots currently retained by the project. The workbook has 38 scored rows
(Excel rows 3–40) and the maximum points sum to exactly 100.

The accounting below is an evidence-coverage estimate, not a predicted or
guaranteed grade. A row is counted in exactly one coverage bucket:

| Coverage bucket | Rows | Maximum points associated with the rows |
|---|---:|---:|
| `FULLY_EVIDENCED` | 22 | 59 |
| `PARTIALLY_EVIDENCED` | 16 | 41 |
| `NO_SUPPORTING_EVIDENCE` | 0 | 0 |
| **Total** | **38** | **100** |

`FULLY_EVIDENCED` can include version-bound historical runtime evidence when
the report identifies the executed source. It does not imply that the latest
integration HEAD reproduced the result or that an assessor awarded the points.
`PARTIALLY_EVIDENCED` means that implementation or useful evidence exists, but
one or more literal acceptance requirements, semantic gates, UI captures or
current-version executions remain open. `NO_SUPPORTING_EVIDENCE` means no
supporting implementation or evidence was found; it does **not** mean that no
literal evidence gaps remain anywhere in the rubric. These are evidence
coverage buckets, not guaranteed grades, and supporting evidence for a row does
not by itself prove that every literal acceptance condition was satisfied.

## 2. Evidence status vocabulary

| Status | Meaning |
|---|---|
| `DOCUMENTED` | A technical report explains the requirement. This is not execution proof. |
| `SOURCE_READY` | Reviewable implementation and configuration exist. |
| `TEST_PASS` | Bounded automated tests passed for the identified source or fixture. |
| `RUNTIME_VERIFIED` | Preserved run-bound evidence proves the stated execution. |
| `BUSINESS_E2E_VERIFIED` | The business-level output was checked across the relevant system boundary. |
| `PARTIAL` | Some acceptance conditions are supported and at least one material condition remains open. |
| `NOT_EXECUTED` | The claimed runtime action did not run. |
| `EVIDENCE_MISSING` | Required evidence was not found. |

## 3. Complete 100-point rubric matrix

The requirement and proof text below is transcribed from the workbook. Minor
line breaks are normalized without changing meaning.

| Excel row | Max | Exact requirement | Workbook acceptance criteria | Report and actual implementation | Test/runtime/UI evidence | Coverage and remaining gap |
|---:|---:|---|---|---|---|---|
| 3 | 2 | `Có sử dụng Docker & Docker Compose` | Document Docker image optimization and compare original and optimized sizes. | [Docker report](../../../../docs/edai1/docker/DOCKER_OPTIMIZATION.md): digest-pinned Dockerfile and hardened Compose service. | Historical build/run exit 0; 381,961,667 → 44,171,603 bytes; two checksum-verified evidence renderings. | **Fully evidenced:** `DOCUMENTED / SOURCE_READY / RUNTIME_VERIFIED`. Docker Desktop UI is optional presentation evidence. |
| 4 | 3 | `Optimize Dockerfile (ví dụ multistage build)` | Same Docker optimization proof scope. | Multi-stage baseline/build/optimized targets, slim runtime, non-root UID 65532 and minimal copied artifact. | Matched smoke hash and counts; 88.44% size reduction. | **Fully evidenced:** `DOCUMENTED / SOURCE_READY / RUNTIME_VERIFIED`. |
| 5 | 2 | `Simulate skew` for at least 100 GB offline data | Document skew columns/ratios, high-cardinality ratios, schemas before/after evolution, duplicate ratio and volume. | [Generator report](../../../../docs/edai1/data_generator/DATA_GENERATION_AND_SIMULATION.md): deterministic 100 GB generator with time/channel/merchant skew controls. | COMPLETE 100,156,470,260-byte manifest; 80.02% merchant skew measured only on 500k fixture. | **Partial:** canonical 100 GB full-column skew/cardinality scan is `NOT_EXECUTED`. |
| 6 | 2 | `Simulate schema evolution` | Included in the row 5 data-characteristics documentation scope. | V1 transactions omit `channel`; V2 adds `channel`. | Non-empty V1/V2 populations and distinct schema fingerprints; historical screenshot. | **Fully evidenced:** `DOCUMENTED / SOURCE_READY / RUNTIME_VERIFIED`. |
| 7 | 2 | `Simulate another offline data problem (Ví dụ: 2% duplicate rate)` | Included in the row 5 data-characteristics documentation scope. | Controlled duplicate injection in the offline generator. | 369,600,000 logical and 376,992,000 physical transaction rows; 7,392,000 added duplicates = 2.00%. | **Fully evidenced:** `DOCUMENTED / SOURCE_READY / RUNTIME_VERIFIED`. |
| 8 | 2 | Store generated data for later Bronze ingestion | Example: MinIO/PostgreSQL simulating another department. | Immutable create-only GCS shard publication with final manifest. | COMPLETE manifest and downstream DP1 consumption. | **Fully evidenced:** `DOCUMENTED / RUNTIME_VERIFIED / BUSINESS_E2E_VERIFIED`. GCS Console capture is missing but machine-readable proof exists. |
| 9 | 2 | `Simulate late arrivals` | Document which data is late, how late and duplicate ratio. | Streaming generator configures 12% late records with 5–1,440 minute delay. | Historical replay: 528 on-time and 72 too-late of 600; terminal evidence. | **Fully evidenced historically:** `DOCUMENTED / SOURCE_READY / RUNTIME_VERIFIED`. |
| 10 | 2 | `Simulate another streaming data problem (Ví dụ: 1.5% duplicate rate)` | Same online-problem documentation scope. | Deterministic duplicate and burst scenarios. | 12,000 logical events plus 180 duplicate arrivals; 12,000 unique accepted in both 100 EPS profiles. | **Fully evidenced historically:** `DOCUMENTED / SOURCE_READY / RUNTIME_VERIFIED`. |
| 11 | 2 | Spark baseline without optimization | Explain each optimization step, use Spark UI screenshots to show problems and explain Airflow integration. | [Spark report](../../../../docs/edai1/spark/OPTIMIZATION_MATRIX.md): version-bound DP1/DP2/DP3 A/B configurations and results. | Run IDs and timings are retained; genuine baseline/optimized DP1 Spark Environment and optimized Stages UI are embedded. | **Partial:** DP1 UI proof is recovered, but DP2/DP3 Spark UI and complete per-stage problem diagnosis remain unavailable. |
| 12 | 3 | Handle skew with explanation | Same Spark proof scope, including observed problem and experiments. | AQE/skew-join and partition profiles are documented. | DP1 optimized was 10.05% slower; DP2 Silver 31.62% slower; no unsupported gain claimed. | **Partial:** experiments are real, but canonical skew metrics and Spark UI diagnosis are incomplete. |
| 13 | 3 | Handle schema evolution with explanation | Same Spark proof scope. | V1/V2 normalization and compatible downstream transformation are implemented and documented. | Schema-evolution source evidence and successful downstream processing exist. | **Partial:** implementation/runtime support exists, but the Spark UI and row-specific experiment chain are not consolidated. |
| 14 | 3 | Handle another offline data problem with explanation | Same Spark proof scope. | Duplicate normalization/deduplication and semantic comparisons are implemented. | DP2 Silver 8/8 tables equivalent; DP2 Gold final semantic equivalence failed for three double aggregates. | **Partial:** Silver proof is strong; the final Gold gate remains `FAILED`, with recovery `NOT_EXECUTED`. |
| 15 | 2 | `Spark job được tích hợp vào các data pipeline` | Same Spark/Airflow proof scope. | Spark Dataproc submissions are wired into DP1, DP2 and DP3 DAGs. | Genuine historical Airflow UI is integrated for DP1 success, DP2 failure with 24/25 tasks successful, and historical DP3 success. DP1 Spark UI configuration/stage captures are also recovered; DP2/DP3 Spark UI remains unavailable. | **Partial:** `SOURCE_READY / RUNTIME_VERIFIED` for bounded stages, but DP2's final semantic gate failed and the newer DP3 configuration was not executed. |
| 16 | 2 | Flink baseline without optimization | Explain optimization steps and use Flink UI screenshots to show and explain problems. | [Flink report](../../../../docs/edai1/streaming/FLINK_STREAMING.md): baseline/optimized 100 EPS profiles and later Block A implementation. | Nine authentic Flink UI screenshots plus comparison; historical benchmark SHA `22c0c7f`. | **Partial:** historical A/B is evidenced, but verdict is mixed and latest implementation commit `0466ba4` was not runtime-verified. |
| 17 | 3 | Handle late arrival with explanation | Same Flink proof scope. | Event-time watermarks, allowed lateness and late/too-late routing. | Boundary tests, legacy output and historical watermark UI. | **Partial:** historically supported; authoritative latest Block A run and complete 100 EPS late-metric artifact are absent. |
| 18 | 3 | Handle another streaming problem with explanation | Same Flink proof scope. | Deduplication, retry/DLQ and burst paths. | 12,180 raw → 12,000 unique accepted at 100 EPS; historical functional checks passed. A separate 500 EPS campaign failed semantic parity. | **Partial:** valid historical proof exists, but latest-source runtime is unverified and the failed 500 EPS campaign must remain visible. |
| 19 | 2 | `Window processing` | Show Flink window-processing code and explain it. | Five-minute tumbling windows and Block A rolling feature windows are documented. | Exact `[T-window,T)` tests, historical topology/watermark UI and 35,975 equal online updates. | **Partial:** source/tests and historical evidence exist; latest implementation runtime is `NOT_EXECUTED`. |
| 20 | 3 | Lakehouse storage optimization, e.g. compaction, z-order or partitioning | Show code and analyze optimized versus unoptimized results. | [Storage report](../../../../docs/edai1/storage/STORAGE_OPTIMIZATION.md): Delta partition pruning and Trino compaction. | Six screenshots; files 2,188 → 151; median 0.2190 → 0.1079 s, 2.03×. | **Fully evidenced:** `DOCUMENTED / RUNTIME_VERIFIED`. |
| 21 | 3 | Datawarehouse optimization, e.g. indexing | Same before/after code and analysis requirement. | [Storage report](../../../../docs/edai1/storage/STORAGE_OPTIMIZATION.md): matched PostgreSQL 16 benchmark and 55 MB partial covering index on merchant/date for completed rows. | Historical local 2M-row fixture; 1 warmup + 7 runs per variant; full plans; median 65.776 → 19.913 ms (69.73%); semantic equivalence PASS; insert overhead +78.14%; checksum-verified raw JSON and rendering. | **Fully evidenced for the historical local workload:** `DOCUMENTED / SOURCE_READY / RUNTIME_VERIFIED`, classified `VERIFIED_WITH_RUNTIME_EVIDENCE`. Not canonical production performance; no authentic DB UI screenshot. |
| 22 | 2 | DP1 ingest stage | Show images of all three Airflow pipelines and explain each pipeline and component. | [Airflow report](../../../../docs/edai1/airflow/AIRFLOW_DP1_DP2_DP3_EVIDENCE.md): manifest → quota → source-to-Bronze → eight-table output. | Genuine run-bound UI shows optimized historical DagRun and submit success; matched baseline/optimized artifacts and logs retained. | **Fully evidenced historically:** `DOCUMENTED / SOURCE_READY / RUNTIME_VERIFIED`. Baseline was selected because optimized was slower; current-source reproduction is optional strengthening. |
| 23 | 2 | DP1 validate stage | Inherits row 22 proof scope. | Source-manifest and exact Bronze Delta-set validators. | Genuine UI shows source-manifest and Bronze Delta validation tasks successful; output/equivalence artifacts retained. | **Fully evidenced historically:** `RUNTIME_VERIFIED`; no claim about a current deployment. |
| 24 | 2 | DP2 Bronze → Silver/Gold ingest stage | Inherits row 22 proof scope. | Current 25-task Method 2 Silver/Gold A/B workflow. | Genuine failed-DagRun UI and artifacts show all baseline/optimized Silver and Gold transformations completed; 24/25 tasks succeeded. | **Fully evidenced for transformation execution:** `RUNTIME_VERIFIED`; overall DagRun remains `FAILED` and no Gold winner was selected. |
| 25 | 2 | DP2 validate stage | Inherits row 22 proof scope. | Physical verify, semantic scan, evidence and winner gates. | Earlier physical/data checks passed; final Gold equivalence task failed; validator fix has 18 local tests. | **Partial:** historical final status is `FAILED`; cloud validation-only recovery and DP2 Gold DataHub check are `NOT_EXECUTED`. |
| 26 | 2 | DP3 offline feature computation ingest stage | Inherits row 22 proof scope. | Silver/Gold verification, feature submission and four-table feature verification. | Genuine UI shows historical `bbae48c` replacement DagRun and all five tasks succeeded after a preserved failed attempt. | **Fully evidenced historically:** `RUNTIME_VERIFIED / HISTORICAL_EXECUTED`. New optimized configuration remains `NOT_EXECUTED`. |
| 27 | 2 | DP3 validate stage | Inherits row 22 proof scope. | Four-table Delta validator plus externally owned PIT validator. | Genuine historical Airflow UI shows the five-task DP3 replacement run succeeded; physical validation passed, and later PIT 310/310 plus four DataHub events passed. | **Partial:** PIT/Feast validation is not a task in the preserved Airflow run, and the newer optimized DP3 configuration remains `NOT_EXECUTED`. |
| 28 | 2 | DP1 lineage between pipeline and tables | Show DataHub tables, producing Airflow pipeline and data validation. | [DataHub report](../../../../docs/edai1/datahub/DATAHUB_GOVERNANCE.md): source manifest → eight Bronze datasets and DP1 DataFlow/DataJob. | Historical aspect read-back and verified screenshot. | **Fully evidenced historically:** `DOCUMENTED / RUNTIME_VERIFIED`. Post-merge reproduction remains optional strengthening. |
| 29 | 2 | DP1 data validation | Same DataHub proof scope. | Source-to-Bronze row reconciliation across eight tables. | `mismatch_count=0`, PASS run event and verified Passed UI screenshot. | **Fully evidenced historically:** `RUNTIME_VERIFIED`. |
| 30 | 2 | DP2 lineage between pipeline and tables | Same DataHub proof scope. | Bronze → Silver → Gold with two DP2 DataJobs. | Historical lineage read-back and three verified screenshots. | **Fully evidenced historically:** `RUNTIME_VERIFIED`. |
| 31 | 2 | DP2 data validation | Same DataHub proof scope. | Two Silver checks and defined Gold row-level integrity check. | Silver checks PASS; Gold definition has no run event. | **Partial:** DP2 Gold assertion remains `NOT_EXECUTED`; dedicated Silver/Gold assertion UI is missing. |
| 32 | 2 | DP3 lineage between pipeline and tables | Same DataHub proof scope. | Silver/Gold → four historical feature datasets and DP3 DataFlow/DataJob. | Historical aspect read-back and verified representative screenshot. | **Fully evidenced historically:** `RUNTIME_VERIFIED`. |
| 33 | 2 | DP3 data validation | Same DataHub proof scope. | Four result-bound PIT assertions. | 310/310 comparisons, zero mismatches and four PASS events in raw/semantic read-back. | **Fully evidenced historically:** `RUNTIME_VERIFIED`; DataHub PIT UI remains unavailable. |
| 34 | 2 | Visualize tables on all zones | Capture tables and relationships in each zone and explain each table. | [Schema report](../../../../docs/edai1/schema/SCHEMA_DESIGN.md): 8 Source, 8 Bronze, 8 Silver and 15 Gold catalog plus Mermaid topology. | Source inventory, contracts and historical/cloud identities; no current all-zone UI/query capture. | **Partial:** `DOCUMENTED / SOURCE_READY`; literal all-zone screenshot is `EVIDENCE_MISSING`. |
| 35 | 2 | Dimension table with SCD2 (`valid_from_ts`, `valid_to_ts`, `is_current`) | Required fields are literal acceptance conditions. | Snapshot-based merchant SCD2 builder, isolated from current Gold table. | Four focused Spark tests and three checksum-listed local-fixture screenshots. | **Partial:** `SOURCE_READY / TEST_PASS`; no real retained snapshots or runtime table. |
| 36 | 2 | Feature tables with `event_timestamp` and `created` | Both columns required on `feat_` tables. | All five builders/contracts expose both while preserving 31 model features. | Sixteen focused tests and four local-fixture screenshots. | **Partial:** `SOURCE_READY / TEST_PASS`; immutable cloud materialization/read-back is `NOT_EXECUTED`. |
| 37 | 2 | Relationship between dimension and fact tables | DBeaver or similar export is suggested. | Logical star joins, validators and Mermaid ERD are documented. | Source/contracts only; no current DBeaver/Trino relationship export. | **Partial:** `SOURCE_READY / EVIDENCE_MISSING`. |
| 38 | 2 | Naming convention (`dim_`, `fact_`, `obt_`, `feat_`; `raw_`, `stg_` or similar) | Zone-specific naming convention must be visible and explained. | Gold prefixes comply; Bronze/Silver use zone-qualified unprefixed names. | Canonical inventories and storage configuration. | **Partial:** assessor must accept zone qualification as “similar”, or explicit `raw_`/`stg_` convention is needed. |
| 39 | 10 | Novel Idea 1 | `Document idea + proof it worked!` | [Deterministic sharding report](../../../../docs/edai1/novel_ideas/01_DETERMINISTIC_SHARDING.md): reproducible shards, resume, create-only publication and final commit marker. | Byte-identical 1/2/4-worker tests and COMPLETE 100,156,470,260-byte, 3,696-shard runtime manifest. | **Fully evidenced:** `DOCUMENTED / SOURCE_READY / TEST_PASS / RUNTIME_VERIFIED / BUSINESS_E2E_VERIFIED`. Formal novelty acceptance remains with assessor. |
| 40 | 10 | Novel Idea 2 | `Document idea + proof it worked!` | [PIT report](../../../../docs/edai1/novel_ideas/02_PIT_LEAKAGE_PREVENTION.md): independent direct-Gold oracle, leakage guard and result-bound governance. | Cloud Run 1 succeeded/0 failed; Cloud Logging 10 × 31 = 310/310, zero mismatch, labels excluded; four DataHub PASS events. | **Fully evidenced:** `DOCUMENTED / SOURCE_READY / TEST_PASS / RUNTIME_VERIFIED / BUSINESS_E2E_VERIFIED`. DataHub UI is unavailable but raw read-back is retained. |

## 4. Runtime and source-version provenance

| Area | Executed provenance and claim boundary |
|---|---|
| Docker | Historical matched A/B artifact; the report records image IDs, timestamps, checksums and source SHA `561910e`. |
| Generator | Canonical GCS manifest generation `1792824840507681`, created 2026-10-24, tied to historical source `bbae48c`; full-run duration is unavailable. |
| Spark DP1 | Method 2 run `ab-20261029174838`; equivalent outputs, baseline selected because optimized was slower. |
| Spark DP2 | Run `dp2-method2-ab-20261031035745`; transformations produced outputs, but `validate_and_select_gold_winner` failed. The local validator fix does not retroactively change the DagRun. |
| Spark DP3 | Successful measurements belong to historical source `bbae48c`; the new optimized profile was not executed. |
| Flink | Authentic 100 EPS UI evidence belongs to source `22c0c7f`; latest Block A implementation is commit `0466ba4` contained by integration `e7dbbc2`, but was not runtime-verified. |
| Airflow | Genuine historical UI is integrated for DP1 success, DP2 failed final DagRun (24/25 tasks successful) and historical DP3 success. A separate removed legacy-DAG image is excluded from rubric proof. |
| DataHub | Historical publication/read-back proves 31 lineage aspects, seven PASS events and one DP2 Gold definition without a run event. |
| Schema rows 35–36 | Verification is local fixture/test evidence, not production Delta/Feast runtime proof. |
| PIT | Cloud Run execution `feast-g3-261030183547-tqqcx`; visible Git SHA `167e807a22bac29df2db8933b6e7d664d3cb3c72`. |
| Warehouse index | Historical isolated local PostgreSQL v2 run, 2026-10-29 14:20 UTC, implementation commit `cf3e867`; synthetic 2M-row fixture and one selective query, not production workload evidence. |

## 5. Verified implementations

- Docker multi-stage optimization has matched semantic smoke output and a
  measured 88.44% image-size reduction.
- The canonical generator published more than 100 GB with deterministic shards,
  explicit V1/V2 schemas and exactly 2% added offline duplicate rows.
- Spark A/B experiments preserve negative results: DP1 and DP2 Silver selected
  baseline, DP2 Gold recorded a faster optimized stage but failed the final
  semantic gate, and the new DP3 profile was not run.
- The lakehouse compaction experiment reduced active files by 93.10% and median
  query time by 50.73% on its documented fixture.
- The PostgreSQL warehouse experiment retained seven-run before/after plans,
  exact semantic equivalence, a 69.73% median read-latency reduction and the
  measured 78.14% write-overhead trade-off.
- Historical DataHub lineage and validation read-back supports DP1, DP2 lineage,
  DP3 lineage and PIT validation. DP2 Gold validation remains unexecuted.
- Both novel ideas have source, bounded tests and runtime evidence. The PIT
  experiment has direct Google Cloud UI proof of 310/310 comparisons with zero
  mismatch and label exclusion.

## 6. Partial or missing requirements

| Priority | Row(s) / points | Gap | Action class | Effort | Reason for priority |
|---:|---|---|---|---|---|
| 1 | 25 and 27 / 4 | Historical Airflow UI is now recovered, but DP2 final gate failed and DP3 PIT validation remains external to the DAG. | **D — runtime execution** only if separately authorized | HIGH | UI recovery cannot repair semantic/runtime ownership gaps. |
| 2 | 34–38 / 10 | All-zone UI/ERD absent; SCD2 and feature timestamps are not runtime-materialized; naming acceptance open. | A/B for rows 34, 37, 38; D for 35–36 | LOW to HIGH | Several low-cost presentation fixes exist, but production proof needs runtime work. |
| 3 | 11–15 / 13 | Spark UI/problem screenshots and consolidated row-specific chains incomplete; DP2 Gold gate failed. | B, then D only if recovery authorized | MEDIUM/HIGH | Strong artifacts already exist, so evidence recovery may improve coverage without a full rerun. |
| 4 | 16–19 / 10 | Historical Flink source differs from latest implementation; latest dual-store runtime is unverified and 500 EPS parity failed. | C for clean focused tests; D for exact-version runtime | MEDIUM/HIGH | Do not overwrite valid historical evidence or infer latest-version verification. |
| 5 | 5 / 2 | Canonical 100 GB skew/cardinality ratios were not scanned. | D — bounded read-only data scan | MEDIUM | The row already has volume/schema/duplicate proof; this closes the literal statistics gap. |
| 6 | 31 / 2 | DP2 Gold DataHub row-level assertion has no run event. | D — validation-only runtime | MEDIUM | Independent from the Airflow final-validation failure and must not be conflated with it. |

No action should be launched from this report. The owner has already stopped
additional DP2/DP3 experiments for the current submission.

## 7. Screenshot inventory

| Report | Embedded screenshots | Coverage note |
|---|---:|---|
| Docker | 2 | Real artifact renderings; not Docker Desktop UI. |
| Data Generator | 4 | 500k skew/schema and historical streaming evidence; no final GCS Console capture. |
| Spark Optimization | 3 | Genuine DP1 baseline/optimized Spark Environment and optimized Stages UI; DP2/DP3 Spark UI remains unavailable. |
| Flink Streaming | 11 | Nine recovered 100 EPS benchmark captures plus two historical terminal captures. |
| Airflow | 4 | One excluded legacy image plus genuine run-bound DP1, failed DP2 and historical DP3 UI captures. |
| Storage | 7 | Six lakehouse captures plus one readable rendering of the historical PostgreSQL runtime artifact. |
| DataHub | 9 | Manifest-listed historical screenshots transferred and checksum-verified. |
| Schema Design | 9 | Includes seven Rows 35–36 local-fixture screenshots; not production runtime. |
| Novel Idea 1 | 1 | Historical schema-evolution context; machine-readable 100 GB manifest is stronger proof. |
| Novel Idea 2 | 2 | Genuine Cloud Run and Cloud Logging UI; DataHub PIT UI unavailable. |
| **Total embedded** | **62** | Each report defines its own provenance and claim boundary. |

## 8. Recommended low-cost actions

1. Preserve the recovered DP1 Spark UI and three historical Airflow UI captures.
   DP2/DP3 Spark UI remains `EVIDENCE_UNAVAILABLE`; do not substitute Airflow or
   Dataproc summary images for Spark UI.
2. Export a current all-zone schema view and dimension/fact ERD from an existing
   read-only query/UI session if one is already available. Do not start services
   solely for screenshots.
3. Ask the assessor whether zone-qualified Bronze/Silver names satisfy “or
   similar” in row 38. This may close a documentation issue without renaming data.
4. Add a single navigation link from the repository documentation index to this
   matrix if the owner authorizes that separate edit.
5. Preserve the submission as-is if the remaining actions require cloud/runtime
   work; the current reports already state the limitations honestly.

## 9. Final documentation index

1. [Docker and Docker Compose Optimization](../../../../docs/edai1/docker/DOCKER_OPTIMIZATION.md)
2. [Data Generation and Simulation](../../../../docs/edai1/data_generator/DATA_GENERATION_AND_SIMULATION.md)
3. [Spark Optimization Matrix](../../../../docs/edai1/spark/OPTIMIZATION_MATRIX.md)
4. [Flink Streaming](../../../../docs/edai1/streaming/FLINK_STREAMING.md)
5. [Airflow DP1/DP2/DP3 Evidence](../../../../docs/edai1/airflow/AIRFLOW_DP1_DP2_DP3_EVIDENCE.md)
6. [Storage Optimization](../../../../docs/edai1/storage/STORAGE_OPTIMIZATION.md)
7. [DataHub Governance](../../../../docs/edai1/datahub/DATAHUB_GOVERNANCE.md)
8. [Schema Design](../../../../docs/edai1/schema/SCHEMA_DESIGN.md)
9. [Novel Idea 1: Deterministic Sharding](../../../../docs/edai1/novel_ideas/01_DETERMINISTIC_SHARDING.md)
10. [Novel Idea 2: PIT Leakage Prevention](../../../../docs/edai1/novel_ideas/02_PIT_LEAKAGE_PREVENTION.md)

## 10. Submission checklist

- [x] All 38 scored workbook rows are represented once.
- [x] Maximum points reconcile to 100.
- [x] Each row links to its primary report and states implementation/evidence.
- [x] Historical runtime is distinguished from latest-source verification.
- [x] DP1 optimized regression and baseline selection are preserved.
- [x] DP2 Silver regression and DP2 Gold observed speedup/final failure are preserved.
- [x] DP3 new optimized profile remains `NOT_EXECUTED`.
- [x] Flink 100 EPS evidence, 500 EPS parity failure and source divergence are preserved.
- [x] Canonical 100 GB duplicate rate and 500k-only skew measurement are separated.
- [x] DP2 Gold DataHub assertion remains `NOT_EXECUTED`.
- [x] PIT 310/310, zero mismatch, label exclusion and four raw DataHub PASS events are retained.
- [x] Missing UI evidence is not replaced with fabricated screenshots.
- [ ] Assessor acceptance and final awarded score remain outside this report.
