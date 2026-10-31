# R4 Sharded Generator, Immutable GCS Source, and Bronze Pilot Plan

> **Execution:** Codex implements and runs local checks. The user runs the
> explicitly identified GCP jobs that create billable resources and returns the
> output/evidence. Do not push unless the user asks.

**Goal:** Produce a deterministic, resumable, bounded-memory source dataset on
GCS and prove the source-to-Bronze path with an approximately 1 GB pilot on
Dataproc Serverless.

**Architecture:** Keep the accepted fraud and transaction semantics inside the
existing offline generator. A sharding layer gives each shard a deterministic
seed and an isolated user/account/device/merchant cohort, writes the existing
nine physical Parquet outputs, validates them, and publishes them beneath one
immutable dataset version. A completed dataset manifest is the only readiness
marker consumed by Spark. A standalone Dataproc job reads every completed
source shard, unions V1/V2 transactions with explicit schema evolution, and
writes eight logical Bronze Delta tables to a run-specific GCS prefix.

**Baseline:** `454a548` (`feat: validate Delta Lake on Dataproc Serverless`)

**Tech stack:** Python 3.10, Pandas/PyArrow for bounded shard generation,
PySpark 3.5.3, Delta Lake 3.2.0, GCS, Dataproc Serverless runtime 2.2,
Terraform outputs, unittest.

## Scope and rulings

- R4 generates and validates a 1 GB source pilot and proves DP1 logic through
  Bronze Delta.
- R4 does not generate 100 GB. The 10 GB full-pipeline pilot belongs to R5,
  after core Gold and historical feature Gold have separate entrypoints.
- The existing generator remains the business-logic authority. Sharding wraps
  it rather than reimplementing fraud scenarios in Spark.
- Each shard is a complete, independent cohort. IDs and references do not
  cross shard boundaries. This bounds memory and makes retry safe.
- `dataset_version + config digest + global_seed + shard_id` determines a
  shard. A retry must either verify the existing shard or fail; it must never
  append duplicate rows.
- Source Parquet is canonical immutable input. Bronze/Silver/Gold remain Delta.
- Generation is an explicit upstream job. Airflow DP1 will consume a completed
  source manifest in R5 and must never call the generator implicitly.
- Existing local MinIO/Trino and the monolithic Airflow DAG are transitional
  until cloud DP1/DP2/DP3 and the 100 GB canonical run pass. R4 inventories
  them but does not delete them.
- Repository deletion/consolidation is a named R6C gate before Feast Online,
  MLflow, Kubeflow, KServe, and gateways.

## Target source layout

```text
gs://<canonical-bucket>/source/<dataset-version>/
├── _control/
│   ├── plan.json
│   ├── shards/shard-00000.json
│   └── dataset_manifest.json          # written only after all shards PASS
├── users/shard-00000.parquet
├── accounts/shard-00000.parquet
├── merchants/shard-00000.parquet
├── devices/shard-00000.parquet
├── transactions_v1/shard-00000.parquet
├── transactions_v2/shard-00000.parquet
├── balance_snapshots/shard-00000.parquet
├── login_events/shard-00000.parquet
└── fraud_labels/shard-00000.parquet
```

The dataset manifest records the source config hash, shard plan hash, file
SHA-256 values, schemas, row counts, logical/physical transaction counts,
fraud scenario counts, total bytes, generator revision, and creation time.

## Target Bronze layout

```text
gs://<canonical-bucket>/lakehouse/runs/<run-id>/bronze/<logical-table>/
```

There are eight logical Bronze tables: users, accounts, merchants, devices,
transactions, balance_snapshots, login_events, and fraud_labels. V1/V2 are
physical source representations of one logical transactions table.

## Task 1: Deterministic shard plan and local bounded generation

**Files:**

- Create: `data_platform/generation/src/sharded/__init__.py`
- Create: `data_platform/generation/src/sharded/models.py`
- Create: `data_platform/generation/src/sharded/planner.py`
- Create: `data_platform/generation/src/sharded/generator.py`
- Create: `tests/unit/test_sharded_generator.py`
- Modify only if required: `data_platform/generation/src/offline/offline_generator.py`

### RED

Add tests proving:

1. the same version, base config, global seed, shard count, and shard ID produce
   the same derived seed and plan digest;
2. changing the version or shard ID changes the derived seed;
3. an invalid shard ID, empty version, non-positive counts, or unsafe output
   path fails before generation;
4. two small shards have no duplicate user, account, device, transaction, or
   label keys;
5. every account/device belongs to a user inside its shard;
6. every transaction reference and every fraud label reference resolves;
7. both transaction schema versions exist and preserve the accepted schemas;
8. all four fraud scenarios are present for a sufficiently large test shard;
9. peak live generation scope is one shard; the API returns file metadata and
   does not return a list containing dataframes for all shards.

Run:

```bash
python -m unittest tests.unit.test_sharded_generator -v
```

Expected: tests fail because the sharded package does not exist.

### GREEN

Implement frozen plan/manifest dataclasses and a pure seed derivation function
using SHA-256. Deep-copy the accepted base YAML per shard, set shard-local
`n_users`, `n_merchants`, `n_transactions`, `random_seed`, and
`fraud.random_seed`, then call `generate_offline_datasets` once for that shard.
Write each dataframe to a temporary shard directory, compute its SHA-256,
schema fingerprint, row count, and byte count, and release dataframe memory
before moving to another shard.

Do not change fraud formulas, temporal cutoffs, schema evolution, duplicate
semantics, balance semantics, or target leakage behavior.

Run the focused test again and then:

```bash
python -m unittest tests.test_fraud_generator tests.test_fraud_label_platform -v
```

Expected: sharded tests and the existing 25 fraud generator/label tests PASS.

### Commit

```bash
git add \
  data_platform/generation/src/sharded/__init__.py \
  data_platform/generation/src/sharded/models.py \
  data_platform/generation/src/sharded/planner.py \
  data_platform/generation/src/sharded/generator.py \
  tests/unit/test_sharded_generator.py
git commit -m "feat: add deterministic bounded source sharding"
```

If `offline_generator.py` required a small extraction, stage it explicitly and
include its existing regression tests in the commit evidence.

## Task 2: Immutable GCS publication, resume, and completed manifest

**Files:**

- Create: `data_platform/generation/src/sharded/storage.py`
- Create: `data_platform/generation/src/sharded/manifest.py`
- Create: `ops/gcp/generate_source_dataset.py`
- Create: `tests/unit/test_source_dataset_publisher.py`
- Modify: `.gitignore` only if a new runtime directory is introduced

### RED

Use a fake object-store boundary and test:

1. dry-run is the default and performs no upload;
2. a completed shard with matching plan/hash is verified and skipped;
3. a partial shard is regenerated and published without appending;
4. an existing object with a conflicting hash fails closed;
5. `dataset_manifest.json` is absent until every expected shard manifest PASS;
6. the final manifest includes all nine physical tables, exact aggregate rows,
   total bytes, schema fingerprints, checksums, and fraud scenario totals;
7. local temporary Parquet is removed only after remote metadata verification;
8. absolute developer paths and credentials never enter JSON evidence;
9. the GCS prefix is derived from runtime config and dataset version, not a
   hard-coded bucket.

Run:

```bash
python -m unittest tests.unit.test_source_dataset_publisher -v
```

Expected: RED before implementation.

### GREEN

Implement a small storage protocol with local and GCS adapters. The GCS
adapter uses the authenticated Google client/ADC, generation preconditions for
immutable writes, and remote metadata/hash verification. The CLI accepts a
base generator config, dataset version, global seed, shard geometry, runtime
environment, optional shard range, staging directory, and `--execute`.

The CLI prints the exact plan and estimated rows before any write. It refuses
to publish over a completed dataset version. Resume is allowed only for absent
shards whose plan digest matches the remote plan.

Run focused tests plus config/secret/path contract tests.

### Commit

```bash
git add \
  data_platform/generation/src/sharded/storage.py \
  data_platform/generation/src/sharded/manifest.py \
  ops/gcp/generate_source_dataset.py \
  tests/unit/test_source_dataset_publisher.py \
  .gitignore
git commit -m "feat: publish immutable source datasets to GCS"
```

Stage `.gitignore` only when it actually changed.

## Task 3: Measurement-based scale calibration and cloud profiles

**Files:**

- Create: `configs/generation/cloud-smoke.yaml`
- Create: `configs/generation/cloud-pilot-1gb.yaml`
- Create: `ops/gcp/calibrate_source_scale.py`
- Create: `tests/unit/test_source_scale_calibration.py`
- Create: `docs/cloud/source_dataset_generation.md`

### RED

Test that calibration:

1. measures actual Parquet bytes from a deterministic calibration shard;
2. reports bytes per logical transaction and bytes by physical table;
3. derives shard count from target bytes rather than accepting an unexplained
   arbitrary final row count;
4. rounds up conservatively and reports expected range/tolerance;
5. rejects a plan that exceeds configured local staging or free-disk margin;
6. never labels a plan 1 GB/10 GB/100 GB unless the measured estimate supports
   that label;
7. records peak RSS, runtime, config hash, and source git revision.

### GREEN

Build a calibration CLI around one bounded shard. Use process resource metrics
and actual Parquet output sizes. Keep the 1 GB profile declarative: target
bytes, safety margin, shard-local row geometry, and allowable size tolerance.
The generated plan JSON contains the measured derived shard count.

The runbook must explain dry-run, calibration, generation, resume, manifest
verification, GCS size checks, cleanup, and cost boundaries. It must state
that the 1 GB pilot is a correctness gate and cannot support the final 100 GB
Spark optimization claim.

### Commit

```bash
git add \
  configs/generation/cloud-smoke.yaml \
  configs/generation/cloud-pilot-1gb.yaml \
  ops/gcp/calibrate_source_scale.py \
  tests/unit/test_source_scale_calibration.py \
  docs/cloud/source_dataset_generation.md
git commit -m "feat: calibrate cloud source dataset scale"
```

## Task 4: Dataproc source-to-Bronze job

**Files:**

- Create: `data_platform/ingestion/spark/__init__.py`
- Create: `data_platform/ingestion/spark/source_to_bronze.py`
- Create: `ops/gcp/submit_source_to_bronze.py`
- Create: `ops/gcp/package_python_job.py`
- Create: `tests/unit/test_source_to_bronze.py`
- Create: `tests/unit/test_submit_source_to_bronze.py`
- Modify: `data_platform/contracts/*.yml` only if physical-path metadata must be
  parameterized; do not change schemas or business rules

### RED

Test the Spark transformation with bounded local fixtures and the submission
builder with mocked Terraform outputs:

1. exactly nine physical source paths become eight logical Bronze tables;
2. V1 transactions gain nullable `channel` and union with V2 by name;
3. duplicates remain in Bronze;
4. fraud labels remain separate from transactions;
5. all other source schemas remain unchanged;
6. source manifest row/hash/schema totals are checked before Spark writes;
7. outputs use a run-specific Bronze root and overwrite only that run root;
8. the Dataproc command pins the proven runtime/Delta JAR checksums, service
   account, subnet, deps bucket, TTL, labels, event-log path, dataset version,
   and run ID;
9. dry-run is default and `--execute` is explicit;
10. the packaged Python archive excludes credentials, local outputs, caches,
    Terraform state/plans, Git data, and personal superpowers documents.

### GREEN

Implement a thin Spark entrypoint that reads source manifest JSON, validates
the expected table set, reads Parquet from GCS without collecting it, writes
Delta tables, and emits machine-readable evidence. Reuse the Delta dependency
resolver proven by R3; extract shared code rather than copy it.

Bronze validation records source and Delta row counts, distinct logical
transaction IDs, physical duplicate count, schema fingerprints, fraud-label
coverage, Delta versions, input/output bytes, file counts, runtime, Spark
version, and GCS URIs with project-safe redaction where needed.

### Commit

```bash
git add \
  data_platform/ingestion/spark/__init__.py \
  data_platform/ingestion/spark/source_to_bronze.py \
  ops/gcp/submit_source_to_bronze.py \
  ops/gcp/package_python_job.py \
  tests/unit/test_source_to_bronze.py \
  tests/unit/test_submit_source_to_bronze.py
git commit -m "feat: ingest immutable cloud source into Bronze Delta"
```

## Task 5: Run smoke and 1 GB cloud gate

No product code is changed while a cloud run is in flight.

### Local preparation by Codex

Run compile, unit, contract, fraud, config, link, secret/path, and diff checks.
Run the generator smoke locally. Produce exact commands for the user only after
the dry-run output is reviewed.

### User-executed cloud steps

1. Run scale calibration.
2. Review derived transaction/shard counts, estimated GCS bytes/runtime/cost,
   and local disk/RAM safety margin.
3. Execute the 1 GB source publication.
4. Verify the completed source manifest and GCS size.
5. Dry-run the source-to-Bronze submission.
6. Execute one Dataproc Serverless Bronze batch.
7. Return the batch ID and terminal output to Codex.

Expected: source manifest PASS, batch state `SUCCEEDED`, eight Bronze Delta
tables, all row/schema/reference/duplicate/label contracts PASS.

### Failure policy

- Failed or partial run prefixes are runtime data, not repository changes.
- Keep failure logs/evidence long enough to diagnose.
- Fix code with a reproducing local test before retrying cloud execution.
- Never overwrite a completed source dataset version.
- Never rerun simply to obtain a prettier runtime number.

## Task 6: Evidence, docs, and cleanup inventory

**Files:**

- Create: `docs/evidence/cloud/r4_source_bronze_pilot.json`
- Create: `docs/cloud/repository_cleanup_inventory.md`
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/cloud/configuration.md`
- Modify: `docs/07_performance_benchmarks.md` if that is the current canonical
  benchmark index; otherwise update the actual current benchmark document

### Evidence requirements

Record measured, not invented:

- dataset version and config/plan hashes;
- calibration rows, peak RSS, runtime, bytes per logical transaction;
- derived shard count and actual source bytes by table;
- source logical/physical rows and four scenario counts;
- Dataproc batch ID/state/runtime/version/resource tier;
- Bronze row counts, schemas, duplicate semantics, label coverage, file counts,
  Delta versions, bytes, and validation result;
- GCS size before/after and cleanup status;
- explicit statements: 10 GB NOT RUN, 100 GB NOT RUN, DP2/DP3 NOT RUN,
  optimization claims NOT YET MADE.

### Cleanup inventory requirements

Classify repository paths as:

1. **KEEP — target architecture:** imported or executed by cloud flow;
2. **KEEP — local development:** deliberate local-only adapter/test support;
3. **TRANSITIONAL:** has a replacement path but parity gate is incomplete;
4. **REMOVE AFTER R6C:** no target consumer after DP1/DP2/DP3 and canonical
   100 GB validation;
5. **RUNTIME/GENERATED:** must be ignored and never committed.

For every transitional/removal candidate, record its current consumer, target
replacement, deletion gate, and verification command. Include at least:

- the monolithic `ewallet_batch_pipeline.py` DAG;
- local MinIO/Trino bootstrap and registration scripts;
- local Airflow runtime files;
- generated local Parquet/Feast/model outputs;
- obsolete top-level or duplicate config paths;
- legacy backup/output directories;
- stale docs that describe local state as current cloud truth.

Do not delete a path merely because its name looks old. Deletion requires no
remaining imports, no DAG/CLI consumer, replacement parity evidence, clean
tests, and updated links.

### Validation

```bash
python -m unittest discover -s tests/unit -v
python -m unittest discover -s tests/contract -v
python -m unittest tests.test_fraud_generator tests.test_fraud_label_platform -v
python -m compileall -q data_platform ops tests
git diff --check
```

Also parse YAML/JSON, verify relative docs links, scan staged files for secrets
and absolute developer paths, and confirm no generated Parquet, GCS download,
JAR, Terraform runtime file, Airflow runtime file, cache, or personal
superpowers file is staged.

### Final commit

Stage only verified R4 code, tests, configs, docs, and measured evidence.

```bash
git commit -m "docs: record R4 GCS to Bronze cloud pilot"
```

Do not push.

## R4 completion gate

R4 is complete only when:

- deterministic shard retry and resume tests PASS;
- existing fraud generator and label semantics PASS;
- the 1 GB size comes from measured calibration;
- source manifest is complete and immutable on GCS;
- Dataproc source-to-Bronze batch succeeds;
- eight Bronze logical tables pass contracts;
- no full dataset is collected into driver/Pandas memory;
- no generated/cloud runtime artifact is tracked;
- evidence and runbooks contain measured values;
- cleanup inventory and R6C deletion gate are documented;
- Trino, Airflow, Redis, MLflow, Kubeflow, KServe, gateways, and GKE
  application workloads remain untouched in this checkpoint. The GKE node
  pool may be temporarily scaled to zero for regional vCPU quota scheduling
  and must be restored after the Dataproc batch.

## Next checkpoint

R5 will split core Gold from historical feature Gold, run the approximately
10 GB Bronze → Silver → core Gold → historical features pilot, then implement
the three Airflow DAGs around the already-proven entrypoints. R6 performs the
100 GB baseline/profile/targeted optimization/optimized canonical sequence.
R6C then removes obsolete compatibility paths and reorganizes the repository
before the online feature and ML platform phases.
