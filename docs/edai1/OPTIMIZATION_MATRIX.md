# EDAI-1 Optimization Matrix and Spark Performance Analysis

## Measurement policy

This report separates stage performance from semantic certification. A failed
Airflow DagRun does not erase completed Spark stage measurements, but a faster
stage is not an approved winner unless its correctness gate also passes.

All Spark timings use `application_runtime_seconds` from preserved Spark event
logs. `batch_runtime_seconds` separately includes Dataproc setup/teardown.
Airflow validation and winner selection are excluded from speedups.

```text
speedup = baseline application runtime / optimized application runtime
improvement % = (baseline - optimized) / baseline × 100
```

## Final Spark A/B comparison

| Stage | Baseline run | Optimized run | Baseline time | Optimized time | Speedup | Performance verdict | Correctness verdict |
| --- | --- | --- | ---: | ---: | ---: | --- | --- |
| DP1 Bronze | `ab-20261029174838-baseline-bronze-r261029174838` | `ab-20261029174838-optimized-bronze-r261029174838` | 845.381 s | 930.358 s | 0.909× | **10.05% regression** | PASS: high-confidence equivalence; baseline selected |
| DP2 Silver | `ab-20261031035745-baseline-silver-r261031035745` | `ab-20261031035745-optimized-silver-r261031035745` | 2,656.612 s | 3,496.508 s | 0.760× | **31.62% regression** | PASS: 8/8 tables equivalent; baseline selected |
| DP2 Gold | `ab-20261031035745-baseline-gold-r261031035746` | `ab-20261031035745-optimized-gold-r261031035746` | 7,810.271 s | 6,005.469 s | 1.301× | **23.11% observed improvement** | **FAILED final semantic equivalence**; no winner |
| DP3 historical (`bbae48c`) | `ab-20261025134342-baseline-feat-r261025171232` | `ab-20261025134342-optimized-feat-r261026025955` | 1,073.903 s | 1,787.068 s | 0.601× | **66.41% historical regression** | Four-table physical checks PASS; not strict common-input Method 2 |
| DP3 new optimized configuration | historical baseline only | `NOT_EXECUTED` | n.a. | n.a. | n.a. | **NOT_COMPARABLE** | `NEW_OPTIMIZED_NOT_EXECUTED` |

DP1 and DP2 use explicit `stage-common-input` manifests. DP2 Gold baseline and
optimized both consume frozen Silver
`ab-20261031035745-baseline-silver-r261031035745`. DP3 is only a qualified
historical comparison: the profiles use the same logical dataset but different
historical Silver/Gold run lineages.

## Effective Spark configuration

Executed jobs used Dataproc Serverless `2.2.89`, Standard tier, four-core
drivers and executors with `9600m` heap. DP1/DP2 also record `3840m` memory
overhead, dynamic allocation `2 / 6 / 7` minimum/initial/maximum executors,
allocation ratio `0.3`, idle timeout `300s`, and broadcast threshold `192m`.

| Stage/profile | AQE | Skew join | Shuffle partitions | Allocation profile | Effective difference |
| --- | :---: | :---: | ---: | --- | --- |
| DP1 baseline | off | off | 200 | default | shared resources/layout |
| DP1 optimized | on | off | 200 | `performance` | AQE and allocation profile |
| DP2 Silver baseline | off | off | 200 | default | shared resources/layout |
| DP2 Silver optimized | off | off | 304 | default | partition count only |
| DP2 Gold baseline | off | off | 200 | default | shared frozen Silver input |
| DP2 Gold optimized | on | off | 400 | default | AQE and partition count |
| DP3 historical baseline | off | off | 200 | default | 6→7 executors |
| DP3 historical optimized | on | on | 400 | `performance` | AQE, skew, partitions, allocation profile |

No cache/persistence difference is recorded. Output file policy stayed
stage-controlled within each strict DP1/DP2 pair. Proposed settings absent from
batch evidence are not treated as effective configuration.

## DP1 — Bronze A/B

Benchmark `ab-20261029174838`, Git SHA
`62f17ba6fe306b5cf74621b787249f8b71ccd8e8`, used shared immutable input
`canonical-100gb-v1`. Both batches `SUCCEEDED`.

| Metric | Baseline | Optimized | Change |
| --- | ---: | ---: | ---: |
| Application runtime | 845.381 s | 930.358 s | **+10.05%** |
| Batch runtime | 915.822 s | 1,007.614 s | +10.02% |
| DCU-hours | 7.232 | 8.057 | **+11.41%** |
| Shuffle-storage GB-s | 2,636,400 | 2,937,200 | **+11.41%** |
| Spill bytes | 0 | 0 | 0 |
| Tasks | 37,390 | 37,195 | -0.52% |
| Output files / bytes | 1,808 / 118,309,481,436 | identical | 0% |

AQE and the `performance` allocation profile did not reduce output work, spill,
or file count. The optimized run consumed more DCU/shuffle storage and was
slower, so baseline was correctly selected after equivalent output was proven.
AQE/allocation overhead is a plausible explanation, but not a proven cause:
per-stage scheduler delay, executor CPU and AQE plan-change evidence are absent.

Raw evidence:
`~/ewallet-evidence/spark-method2-final/ab-20261029174838/postrun-check/`.

### Recovered DP1 Spark UI evidence

The following genuine Google Cloud Managed Apache Spark UI captures belong to
campaign `ab-20261029174838`, Git SHA
`62f17ba6fe306b5cf74621b787249f8b71ccd8e8`. They strengthen the configuration
and stage evidence for DP1 only; they do not provide missing DP2/DP3 Spark UI
proof or establish why the optimized run regressed.
The copied files are byte-identical to their historical sources; SHA-256 values
are recorded in `docs/evidence/edai1/spark/SHA256.txt`.

![DP1 baseline Spark environment showing AQE disabled](../evidence/edai1/spark/01_dp1_baseline_spark_environment.png)

*Baseline Spark Environment view for batch
`ab-20261029174838-baseline-bronze-r261029174838`, showing
`spark.sql.adaptive.enabled=false` and `spark.sql.shuffle.partitions=200`.*

![DP1 optimized Spark environment showing AQE enabled](../evidence/edai1/spark/02_dp1_optimized_spark_environment.png)

*Optimized Spark Environment view for batch
`ab-20261029174838-optimized-bronze-r261029174838`, showing
`spark.sql.adaptive.enabled=true`. The UI proves the effective setting, not a
causal performance benefit.*

![DP1 optimized Spark completed stages](../evidence/edai1/spark/03_dp1_optimized_spark_stages.png)

*Optimized Spark Stages view showing 81 completed, 40 skipped and zero failed
stages for the captured application. Per-stage scheduler delay, CPU and AQE
plan-change evidence are not present, so the regression cause remains
unresolved.*

## DP2 — independent Silver and Gold comparisons

Campaign `dp2-method2-ab-20261031035745`, dataset `canonical-100gb-v1`, Git
SHA `e58a5006296839730e7ecf96ec2eb5fe4d76889b`, ran sequential Dataproc
batches. Its 06:55:43 Airflow duration is not used in stage speedups.

### Silver baseline versus optimized

Both runs consume Bronze
`ab-20261029174838-baseline-bronze-r261029174838`, produce the same eight
tables, and use the same resources. Only shuffle partitions change 200→304;
AQE/skew join remain off.

| Metric | Baseline | Optimized | Change |
| --- | ---: | ---: | ---: |
| Application runtime | 2,656.612 s | 3,496.508 s | **+31.62%** |
| Batch runtime | 2,717.600 s | 3,571.119 s | +31.41% |
| DCU-hours | 20.411 | 30.429 | **+49.09%** |
| Shuffle-storage GB-s | 7,440,800 | 11,093,200 | **+49.09%** |
| Spill bytes | 187,237,998,918 | 187,975,189,894 | +0.39% |
| Tasks | 6,819 | 8,379 | **+22.88%** |
| Output files | 763 | 763 | 0% |
| Output bytes | 159,605,536,002 | 159,697,306,859 | +0.057% |

The higher partition count created 1,560 more tasks without reducing spill or
file count. Runtime and DCU regressed, consistent with extra scheduling/shuffle
overhead. All eight tables passed equivalence and baseline was selected.

This is not stable across campaigns. Historical `ab-20261029174838` produced
the opposite ordering: optimized 2,630.6 s versus baseline 3,495.7 s. Thus the
evidence supports this run's regression, not a general causal claim about 304
partitions; run-to-run variance remains unisolated.

### Gold baseline versus optimized

Both batches consume exactly the same frozen Silver run. Resources, broadcast
threshold, output scope and file policy match. Optimized enables AQE and raises
shuffle partitions 200→400; skew join remains off.

| Metric | Baseline | Optimized | Change |
| --- | ---: | ---: | ---: |
| Application runtime | 7,810.271 s | 6,005.469 s | **-23.11%** |
| Batch runtime | 7,885.340 s | 6,067.432 s | -23.05% |
| DCU-hours | 68.011 | 52.291 | **-23.11%** |
| Shuffle-storage GB-s | 24,794,000 | 19,063,200 | **-23.11%** |
| Spill bytes | 681,863,335,181 | 450,370,138,926 | **-33.95%** |
| Tasks | 10,050 | 10,443 | +3.91% |
| Output files | 969 | 969 | 0% |
| Output bytes | 235,054,118,567 | 235,108,401,479 | +0.023% |

More tasks accompanied 34% less spill and approximately 23% less runtime,
DCU and shuffle storage. This is consistent with AQE/finer initial partitioning
reducing intermediate pressure, but no retained plan detail isolates an AQE
rule. The baseline also lost one executor and logged `FetchFailedException`;
Spark recomputed and succeeded. That recovery is a material confounder, so the
23.11% result is a valid historical observation, not a repeated causal estimate.

### Correctness qualification

Both Gold transformations completed and physical/data checks passed within
their recorded scope. Final `validate_and_select_gold_winner` still **FAILED**:

- 11/13 tables matched exact fingerprints;
- `feat_user_90d` and `opt_merchant_performance` differed in three `double`
  sum/average columns;
- row/schema/null/key and non-floating fields matched;
- maximum relative difference was about `2.6e-15`;
- the observed values are consistent with IEEE-754 reduction-order sensitivity.
  A different aggregation order caused by AQE or partitioning is a plausible
  hypothesis, not a demonstrated cause, because matching physical execution
  plans and reduction-order evidence were not retained.

The local validator fix passed 18 tests, but cloud validation-only recovery was
`NOT_EXECUTED`. It does not retroactively change the failed DagRun. No official
Gold winner exists. The independent DataHub Gold row-level assertion also
remains `NOT_EXECUTED`.

Evidence: `docs/evidence/dp2_method2_ab-20261031035745/`.

## DP3 — historical configuration only

Executed evidence belongs to Git SHA
`bbae48caef7f65fda0c9375a7a49afb2eca5eef4` and benchmark
`ab-20261025134342`.

| Metric | Historical baseline | Historical optimized | Change |
| --- | ---: | ---: | ---: |
| Application runtime | 1,073.903 s | 1,787.068 s | **+66.41%** |
| Batch runtime | 1,123.141 s | 1,846.347 s | +64.39% |
| DCU-hours | 9.196 | 15.390 | **+67.35%** |
| Shuffle-storage GB-s | 3,352,400 | 5,610,400 | **+67.35%** |
| Output scope | 4 feature tables | 4 feature tables | same logical scope |

The historical optimized profile enabled AQE, skew join, 400 partitions and
the `performance` allocation profile. It was slower and costlier. Added
adaptive/partition/allocation overhead is plausible for this four-table
workload, but missing task/spill/utilization metrics prevent causal attribution.

This is not strict Method 2 common-input: baseline and optimized use separate
historical Silver/Gold lineages. They share the logical dataset and output
scope, but physical input differences reduce causal confidence. Both exact
four-table Delta-set checks passed.

The first optimized DagRun `...-r261026024739` failed pre-submit due to an
unbound feature run ID and has no Spark timing. Replacement
`...-r261026025955` succeeded and owns the optimized timing above. The newer
optimized configuration remains **`NEW_OPTIMIZED_NOT_EXECUTED`**; no runtime or
gain is assigned to it.

Evidence: `docs/evidence/dp3_historical_features_ab-20261025134342/`.

## Comparison validity and remaining gaps

| Check | DP1 | DP2 Silver | DP2 Gold | DP3 historical |
| --- | --- | --- | --- | --- |
| Same logical dataset | yes | yes | yes | yes |
| Exact same upstream run | immutable dataset ID | yes | yes | **no** |
| Same output scope/resources | yes | yes | yes | nominally yes |
| Comparable application boundary | yes | yes | yes | yes |
| Repeated clean trials | no | conflicting prior pair | no; baseline fetch failure | no |
| Correctness | PASS | PASS | **FAILED** | physical table-set PASS only |

Remaining evidence gaps:

1. DP1 Spark Environment and Stages UI is now recovered, but no complete Spark
   UI/per-stage CPU and scheduler metrics exist for all DP1/DP2/DP3 pairs.
2. DP1 AQE-overhead explanation remains a hypothesis.
3. DP2 Silver cross-run instability was not isolated by repeated trials.
4. DP2 Gold baseline executor loss confounds the observed improvement.
5. DP2 final semantic gate remains failed; no winner exists.
6. DP3 lacks identical physical upstream input and task/spill/output metrics.
7. DP3's newer optimized configuration was not executed.

## Non-Spark optimization summary

| Optimization | Baseline | Optimized | Semantic gate | Result / state |
| --- | ---: | ---: | --- | --- |
| Docker image | 381,961,667 bytes | 44,171,603 bytes | identical smoke hash | -88.44%, verified locally |
| Warehouse index v2 (authoritative Row 21 result) | median 65.776 ms | median 19.913 ms | exact query result and table signature | 69.73% lower latency; insert overhead +78.14%; isolated local 2M-row fixture |
| Delta layout | 2,188 files, 0.2190 s | 151 files, 0.1079 s | row/hash equality | local bounded proof; cloud A/B blocked by cost gate |
| DataHub publication | no physical cloud edges | 31 deterministic edges | 42/42 aspect readback | live local GMS verified |

The earlier Warehouse campaign (`37.940 → 10.414 ms`, 72.55% lower latency,
122.25% write overhead) remains a separate historical experiment. It is not the
canonical Row 21 result and is not combined with the v2 measurements above. See
`storage/STORAGE_OPTIMIZATION.md` for the authoritative v2 methodology and raw
artifact references.

The warehouse result is not a recommendation to use MLflow's metadata database
as an analytics warehouse. The cloud Delta harness must use a bounded scratch
subset; the full-table clone path was intentionally not run.
