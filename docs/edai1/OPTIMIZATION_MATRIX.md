# Optimization matrix

| Optimization | Baseline | Optimized | Semantic gate | Result / state |
|---|---:|---:|---|---|
| Docker image | 381,961,667 bytes | 44,171,603 bytes | identical smoke hash | -88.44%, verified locally |
| Warehouse index | median 37.940 ms | median 10.414 ms | result equality | 72.55% lower latency; insert overhead +122.25% |
| Delta layout | 2,188 files, 0.2190 s | 151 files, 0.1079 s | row/hash equality | local bounded proof; cloud A/B blocked by cost gate |
| DataHub publication | no physical dataset-to-dataset cloud edges | 31 deterministic edges | 42/42 aspect readback | verified against live local GMS, repeated payload hash identical |

The warehouse result is not a recommendation to use the MLflow metadata database as an analytics warehouse. The cloud Delta harness must use a bounded scratch subset; the existing full-table clone path is intentionally not executed.

