# EDAI-1 Docker and Storage Optimization: owner evidence runbook

This runbook is for the owner, run on Ubuntu (bash). It collects the official evidence for:
- [DOCKER_OPTIMIZATION.md](../../edai1/docker/DOCKER_OPTIMIZATION.md): rows 3–4, checkpoints DK01–DK05;
- [STORAGE_OPTIMIZATION.md](../../edai1/storage/STORAGE_OPTIMIZATION.md): rows 20–21, checkpoints ST01–ST09 and WH01–WH06.

Every experiment reads the 5GB campaign `ab-20261110225456` (`rehearsal-5gb-v1`, optimized profile).

| Stage | What | Writes to | Paid | Approx. time |
|---|---|---|---|---|
| 0 | Preflight | nothing | no | 5 min |
| 1 | Docker A/B and Compose (DK01–DK05) | local images `edai1/campaign-verifier:*` | no | 10 min |
| 2 | PostgreSQL warehouse (WH01–WH06) | local volume `edai1-wh5gb-evidence-pgdata` (official instance) | no (about 0.3 GB of GCS reads) | 30 min |
| 3 | Lakehouse plan and dry run | local files only | no | 10 min |
| 4 | Lakehouse benchmark (ST01–ST09) | `gs://…rehearsal-5gb-data/lakehouse/benchmarks/storage/<id>/` | **yes: one Dataproc batch** | 60–90 min |
| 5 | Evidence integration (after the screenshots exist) | repository | no | — |

**Order and memory.**
- Run the heavy services one at a time: PostgreSQL in Stage 2, the schema-viz Trino in Stage 4, never DataHub at the same time. Check `free -g` before each stage.
- Other workstreams' containers (`ewallet-flink-bench-*`, `kind-*`) may be running. This runbook never stops them; stop your own if memory is short.
- No command here prunes Docker or deletes a volume, except the explicitly marked owner-approved cleanup steps.

## Stage 0 — Preflight (every shell)

```bash
# Use the checkout that contains the commit under evaluation (the feature worktree before merge, main after).
export REPO="$HOME/Develop/ewallet-edai1-docker-storage-5gb"
cd "$REPO"
git status --short --branch                       # expect a clean tree on the branch or main
export TOOL_GIT_SHA="$(git rev-parse HEAD)"
export UV=/home/huyy-giaa/miniconda3/envs/fintech/bin/uv
export UV_PROJECT_ENVIRONMENT="$HOME/.cache/ewallet-venvs/feast"   # locked env, outside the checkout
export PYTHONPATH="$REPO"
export E=docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456
export SPEC=images/edai1-campaign-verifier/workload-5gb.json
export RUN_TS="$(date -u +%Y%m%dt%H%M%S | tr 'T' 't')"
export OUT="$HOME/ewallet-evidence/edai1-docker-storage-$RUN_TS"; mkdir -p "$OUT"

$UV lock --check --project ai_platform/features/feast            # "Resolved … packages"
$UV sync --locked --project ai_platform/features/feast
python3 -m ops.edai1_verifier --spec "$SPEC" --evidence-dir "$E"  # {"verdict": "PASS", "checks": 45, …}
docker version --format '{{.Server.Version}}'; docker compose version --short
df -h "$HOME" | tail -1                                           # at least 12 GB free
free -g | sed -n 2p                                               # at least 6 GB available
ss -ltn | grep -E ':(55432|8091) ' || echo "ports 55432 and 8091 free"
gcloud auth list --filter=status:ACTIVE --format='value(account)'
gcloud auth application-default print-access-token > /dev/null && echo ADC-OK
```

**PASS:** the lock check passes, the verifier prints `PASS`, both ports are free, `ADC-OK` is printed and there is enough disk.

**FAIL:**
- **Verifier `FAIL`:** the committed evidence was changed. Stop.
- **A port is in use:** find the owner with `docker ps --filter publish=55432`, and stop only your own stack.

## Stage 1 — Docker A/B and Compose (rows 3–4)

```bash
python3 -m ops.docker_ab --run-id "$RUN_TS" --output-dir "$OUT/docker" | tee "$OUT/docker_stdout.txt"
echo "exit=$?"
docker image ls edai1/campaign-verifier
```

**Expected output:**
- a four-row table: `baseline`, `baseline-slim`, `optimized-full`, `optimized`;
- every verdict `PASS`, with one shared `report_sha256` (the summary prints `functional equivalence: PASS`);
- `compose: … run_exit=0 verdict=PASS residual_containers=[]`;
- exit code 0.

**FAIL:**
- **Any variant `FAIL`:** read `$OUT/docker/result.json` → `variants.<name>.functional.runs[*].stderr_tail`.
- **`NOT_DETERMINISTIC`:** the inputs changed during the run; rerun with a new `--run-id`.
- **A build error:** read `$OUT/docker/build_<variant>.log`.

| ID | Rubric | Screen and command | Must be visible | Caption (suggested) |
|---|---|---|---|---|
| DK01 | 4 | Editor, split view: `images/edai1-campaign-verifier/Dockerfile.baseline` and `Dockerfile.optimized` | One `FROM` against `AS build`/`AS runtime`; the full and slim digests; `COPY . /app` against `COPY --from=build`; no `USER` against `USER 65532:65532` | *Single-stage baseline (full Bookworm, whole repository, root) against multi-stage optimized (slim runtime, validated minimal payload, non-root).* |
| DK02 | 4 | Terminal: `tail -n 4 "$OUT"/docker/build_baseline.log "$OUT"/docker/build_optimized.log` | `naming to docker.io/edai1/campaign-verifier:…` and `[exit code 0]` for both | *Both variants built from the same commit with `--no-cache`.* |
| DK03 | 4 | Terminal: `cat "$OUT/docker/summary.txt"` and the `docker image ls edai1/campaign-verifier` output | Content bytes, disk usage and layers per variant; the base-image and packaging effects | *Measured from `docker image inspect`: the content size of each variant and the split between base-image and packaging effects.* |
| DK04 | 4 | Terminal: the two `docker run` commands below | The same `report_sha256` and `"verdict": "PASS"` from both | *Same workload, same read-only inputs, identical 45-check report fingerprint.* |
| DK05 | 3 | Terminal: the Compose commands below | `read_only: true`, `network_mode: none`, `cap_drop: ALL` in `config`; `"verdict": "PASS"`; `exit=0`; empty `ps -a` | *Hardened one-shot Compose run of the optimized image on read-only campaign evidence.* |

```bash
# DK04: one run of each image on identical read-only mounts
for v in baseline optimized; do
  docker run --rm --network none --read-only --cap-drop ALL \
    --mount type=bind,source="$REPO/$E",target=/input/evidence,readonly \
    --mount type=bind,source="$REPO/$SPEC",target=/input/workload.json,readonly \
    "edai1/campaign-verifier:$v-$RUN_TS" --spec /input/workload.json --evidence-dir /input/evidence
done
# DK05: Compose
docker compose -p edai1-dk5gb -f infra/docker/compose.edai1.yml --profile edai1 config | sed -n '1,45p'
docker compose -p edai1-dk5gb -f infra/docker/compose.edai1.yml --profile edai1 run --rm campaign-verifier; echo "exit=$?"
docker ps -a --filter label=com.docker.compose.project=edai1-dk5gb
```

**Provenance:** `$OUT/docker/result.json` holds the tool Git SHA, Docker versions, base-image digests and the campaign identity.

**Safe cleanup (owner-approved, optional).** This removes only this run's tags:
```bash
docker image rm $(docker image ls edai1/campaign-verifier --format '{{.Repository}}:{{.Tag}}' | grep -- "-$RUN_TS")
```

## Stage 2 — PostgreSQL warehouse (row 21)

The official evidence uses its own PostgreSQL instance. The earlier verification
instance (container `edai1-wh5gb-postgres`, volume `edai1-wh5gb-pgdata`, Compose
project `edai1-wh5gb`) is kept as it is: never start, remove or prune it here.

| | Earlier verification (kept) | Official evidence |
|---|---|---|
| Compose project | `edai1-wh5gb` | `edai1-wh5gb-official` |
| Container | `edai1-wh5gb-postgres` | `edai1-wh5gb-evidence-postgres` |
| Volume | `edai1-wh5gb-pgdata` | `edai1-wh5gb-evidence-pgdata` |
| Port | `127.0.0.1:55432` | `127.0.0.1:55432` (same port, so only one runs at a time) |

Both use the same Compose file, image digest and PostgreSQL settings. The
official override changes only the container and volume names. The warehouse
tooling sends all SQL (`load`, `before`, `index`, `after`) through
`docker exec "$EDAI1_WH_CONTAINER" psql`. When the variable is unset it falls
back to the earlier container, so export it in every shell used for this stage.

**2.1 Select the official instance and its password.** PostgreSQL applies the
password only when the volume is first initialised. The password is therefore
created once, next to the override (mode 600), and reused on every later run.
It is never printed.

```bash
export WH_DIR="$HOME/ewallet-evidence/warehouse-5gb-official"
WH_COMPOSE=(docker compose -p edai1-wh5gb-official
            -f infra/docker/compose.warehouse-5gb.yml -f "$WH_DIR/compose.override.yml")   # a shell array: define it in every shell
export EDAI1_WH_CONTAINER=edai1-wh5gb-evidence-postgres
if [ ! -s "$WH_DIR/pg-password" ]; then
  if docker volume inspect edai1-wh5gb-evidence-pgdata >/dev/null 2>&1; then
    echo "STOP: the official volume exists but its password file is missing"   # do not delete; ask
  else
    (umask 077; openssl rand -hex 16 > "$WH_DIR/pg-password")
  fi
fi
chmod 600 "$WH_DIR/pg-password"
export EDAI1_WH_PG_PASSWORD="$(cat "$WH_DIR/pg-password")"
```

**2.2 Check isolation, then start only the official instance.**

```bash
docker ps --filter name='^edai1-wh5gb-postgres$' --format '{{.Names}} {{.Status}}'   # expect no output (earlier instance stopped)
ss -ltn | grep ':55432 ' && echo "STOP: 55432 is in use" || echo "port 55432 free"
"${WH_COMPOSE[@]}" config --format json | python3 -c 'import json,sys;c=json.load(sys.stdin);s=c["services"]["postgres"];print(c["name"], s["container_name"], [v["name"] for v in c["volumes"].values()])'
#   expect: edai1-wh5gb-official edai1-wh5gb-evidence-postgres ['edai1-wh5gb-evidence-pgdata']
"${WH_COMPOSE[@]}" up -d --wait
docker ps --filter name='^edai1-wh5gb-evidence-postgres$' --format '{{.Names}} {{.Status}} {{.Ports}}'
docker inspect edai1-wh5gb-evidence-postgres --format '{{range .Mounts}}{{.Name}}{{end}}'   # edai1-wh5gb-evidence-pgdata
```

**2.3 Extract, load and measure without the index.** `extract` reads only GCS
and writes a local Parquet snapshot. The other commands print
`warehouse target container: edai1-wh5gb-evidence-postgres` to stderr. Stop if
any of them prints a different container.

```bash
$UV run --locked --project ai_platform/features/feast python -m data_platform.storage.warehouse extract \
  --campaign-evidence-dir "$E" --workload-spec "$SPEC" --days 31 --output-dir "$OUT/wh-snapshot"
$UV run --locked --project ai_platform/features/feast python -m data_platform.storage.warehouse load \
  --snapshot-dir "$OUT/wh-snapshot"
python3 -m data_platform.storage.warehouse before --run-dir "$OUT/wh-run"
```

**Expected output:**
- `extract` prints the snapshot (31 partitions; rows equal to the Delta statistics) and the profile;
- `load` prints `rows_loaded` equal to the snapshot rows;
- `before` prints the no-index median and the path of `$OUT/wh-run/dbeaver_session.sql`.

**2.4 DBeaver connection.** Keep the password out of every screenshot:
1. *Database → New Database Connection → PostgreSQL*.
2. Set host `127.0.0.1`, port `55432`, database `warehouse` and user `warehouse`.
3. For the password, copy it with `xclip -selection clipboard < "$WH_DIR/pg-password"` and paste it into the masked field. Do not `cat` the file in a terminal you will capture.
4. Name the connection `edai1-wh5gb-official`. *Test Connection*, then *Finish*.
5. Check that you are on the official instance: `SELECT inet_server_addr(), current_database(), pg_postmaster_start_time();`. The start time must match `docker inspect -f '{{.State.StartedAt}}' edai1-wh5gb-evidence-postgres` to within a few seconds.
6. Open `$OUT/wh-run/dbeaver_session.sql` in the SQL editor (*File → Open File*). Run each block with Ctrl+Enter, in order.

| ID | Block | Must be visible | PASS | Caption (suggested) |
|---|---|---|---|---|
| WH01 | `-- WH01` | `benchmark_provenance` rows (benchmark, Delta version, partitions, snapshot SHA-256); row count; column types | Row count equals `snapshot.rows` | *PostgreSQL table loaded from 31 daily partitions of the 5GB Gold `fact_transactions` (Delta v0 of run `…-optimized-gold-…`).* |
| WH02 | `-- WH02` (run **before** WH03) | Only `fact_transactions_pkey` in `pg_indexes`; a plan with `Seq Scan` or `Parallel Seq Scan`; `Execution Time` | No secondary index | *Before: the merchant/week query scans the whole table.* |
| WH03 | `-- WH03` | The `CREATE INDEX` statement and its execution time in the *Statistics* tab, then `pg_get_indexdef` and the size | Definition equals the design in `before.json` | *Covering B-tree on (merchant_id, event_date) INCLUDE (status, amount), designed from the measured selectivity.* |
| WH04 | `-- WH04` | `Index Only Scan using idx_fact_transactions_merchant_date`; `Execution Time` | Index-only scan chosen | *After: an index-only scan answers the same query.* |

**2.5 Indexed measurements.** Run these after WH03 has created the index in
DBeaver, in the same shell (with `EDAI1_WH_CONTAINER` still exported):

```bash
python3 -m data_platform.storage.warehouse index --run-dir "$OUT/wh-run" --already-created
python3 -m data_platform.storage.warehouse after --run-dir "$OUT/wh-run"
```

| ID | Block | Must be visible | PASS | Caption (suggested) |
|---|---|---|---|---|
| WH05 | `-- WH05` | Run counts, the median, minimum and maximum `execution_ms` and the access path for `read` and `interleaved` | 11 runs per read side, 10 per interleaved side | *Measured latency before and after indexing, including the cache-fair interleaved comparison.* |
| WH06 | `-- WH06` | The median insert time and the WAL per side; `same_rows = true`; the same query result; index size against table size | `same_rows` is true | *Write cost of maintaining the index on identical real-row batches, and semantic equivalence.* |

**FAIL:**
- **`index differs from the design`:** WH03 was edited. Drop the index (`DROP INDEX idx_fact_transactions_merchant_date;`) and rerun WH03 from the generated file.
- **`indexing changed the table`:** stop and keep `$OUT/wh-run` for analysis.
- **A command reports `warehouse target container: edai1-wh5gb-postgres`:** `EDAI1_WH_CONTAINER` is not exported in that shell. Export it (2.1) and rerun the command with a new `--run-dir`.

**2.6 Stop only the official instance.** Its data is kept, and the earlier
verification instance is not touched:

```bash
"${WH_COMPOSE[@]}" stop
docker ps -a --filter name=edai1-wh5gb --format '{{.Names}} {{.Status}}'   # both instances Exited
docker volume ls --filter name=edai1-wh5gb --format '{{.Name}}'           # both volumes still present
```

## Stage 3 — Lakehouse plan and dry run (row 20)

```bash
export STORAGE_BENCHMARK_ID="storage-5gb-$RUN_TS"
./scripts/inspect_storage_layout.sh \
  --benchmark-id "$STORAGE_BENCHMARK_ID" \
  --dataset-version rehearsal-5gb-v1 \
  --source-spark-git-sha aae22b08eee20fa055c4aefc8d23c14cc3ae5f8c \
  --tool-git-sha "$TOOL_GIT_SHA" \
  --data-bucket ewallet-data-ai-platform-hg26-rehearsal-5gb-data \
  --campaign-evidence-dir "$E" --workload-spec "$SPEC" \
  --table dp2-gold.fact_transactions --table dp2-gold.obt_transaction_enriched \
  --output-dir "$OUT/storage-plan"
terraform -chdir=infra/terraform/gcp/environments/dev output -json > "$OUT/terraform-output.json"
./scripts/run_storage_benchmark.sh --plan "$OUT/storage-plan/plan.json" \
  --terraform-output-json "$OUT/terraform-output.json" | tee "$OUT/storage-dry-run.txt"
python3 -m ops.gcp.storage_benchmark_report trino-sql --plan "$OUT/storage-plan/plan.json" --output-dir "$OUT/storage-trino"
```

**Expected output:**
- One JSON line per table, with Delta version 0, 151 files and the data-derived parameters.
- The dry run prints `EXECUTION: DRY_RUN`, the benchmark root under `…rehearsal-5gb-data/lakehouse/benchmarks/storage/`, three arms per table, the workload and the full `gcloud dataproc batches submit` command.

**Review before approving.** Check that:
- `INPUT` names `ab-20261110225456-optimized-gold-r261110225458` at `@v0`;
- every output path is under the new benchmark root;
- `TOOL_GIT_SHA` equals `git rev-parse HEAD` and differs from `SOURCE_SPARK_GIT_SHA`.

**FAIL:**
- **`tool Git SHA differs` / `tracked worktree must be clean`:** commit or check out the exact commit. Do not bypass the gate.
- **`campaign evidence does not verify`:** stop.

**Cost.** One Standard-tier batch: three clones (metadata only), one compaction (expected to rewrite nothing), one Z-order rewrite of about 7.9 GB, and repeated reads. For scale, the campaign's comparison batch used 7.2 DCU-hours. Sleep the GKE lab before submitting: the campaign's first run failed its quota check with the lab awake.

## Stage 4 — Lakehouse benchmark (owner approval required)

```bash
# OWNER APPROVAL: submits one paid Dataproc batch and writes only below the new benchmark root.
./scripts/run_storage_benchmark.sh --plan "$OUT/storage-plan/plan.json" \
  --terraform-output-json "$OUT/terraform-output.json" --execute | tee "$OUT/storage-execute.txt"
gcloud dataproc batches describe "$STORAGE_BENCHMARK_ID-storage" --region=asia-southeast1 \
  --format='value(state,stateTime)'
gcloud storage cp -r \
  "gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data/lakehouse/benchmarks/storage/$STORAGE_BENCHMARK_ID/evidence" \
  "$OUT/storage-evidence"
python3 -m ops.gcp.storage_benchmark_report summary --plan "$OUT/storage-plan/plan.json" \
  --evidence-dir "$OUT/storage-evidence/evidence" | tee "$OUT/storage-summary.txt"
```

**PASS:**
- the batch state is `SUCCEEDED`;
- `summary` prints `equivalence=True` per table, the three comparison arms with their verdicts, and the pruning files read.

Whatever the verdicts (IMPROVED, NO_CLEAR_IMPROVEMENT, REGRESSED or INCONCLUSIVE), they are the result. Do not rerun to change them. **FAIL:** the batch failed or the summary raises. Keep everything, and retry only with a new `STORAGE_BENCHMARK_ID`. Never reuse or delete a partial prefix.

**Spark UI (ST03, ST06).** Open *Google Cloud console → Dataproc → Serverless → Batches → `<id>-storage` → View Spark UI* (or *Batch details → Spark History*):
- ST03: the *SQL / DataFrame* tab with the `OPTIMIZE` query on the `compacted` path, and the *Jobs* tab showing its duration.
- ST06: the same for the `OPTIMIZE … ZORDER BY (merchant_id)` query on the `optimized` path.

**Trino and DBeaver (ST01, ST02, ST04, ST05, ST07, ST09):**
```bash
export EDAI1_GCP_ADC_FILE="$HOME/.config/gcloud/application_default_credentials.json"
docker compose -f infra/docker/schema-viz/compose.yml up -d
until [ "$(docker inspect -f '{{.State.Health.Status}}' edai1-schema-viz-trino)" = healthy ]; do sleep 5; done
infra/docker/schema-viz/run_sql.sh registrar "$OUT/storage-trino/storage_register.sql" | tee "$OUT/storage-register.txt"
```

In DBeaver, connect to *Trino*: host `localhost`, port `8091`, user `viewer`, no password. Open `$OUT/storage-trino/storage_queries.sql` and run each labelled block.

| ID | Screen | Must be visible | Caption (suggested) |
|---|---|---|---|
| ST01 | DBeaver, `$history` of the three arms | `CLONE` at version 0 with the source path and `sourceVersion 0`; then `OPTIMIZE` on `compacted`/`optimized` | *Three isolated shallow clones of the pinned 5GB Gold snapshot.* |
| ST02 | DBeaver, layout of `baseline` | 151 files, about 23 MiB average, 151 partitions | *Before: one file per daily partition.* |
| ST03 | Spark UI, compaction job | The OPTIMIZE job and its duration | *Compaction executed on the isolated `compacted` arm.* |
| ST04 | DBeaver, layout of `compacted` and `optimized` | Files and sizes after each rewrite | *After: the compaction arm is unchanged if OPTIMIZE was a no-op; the Z-order arm was rewritten.* |
| ST05 | DBeaver `EXPLAIN ANALYZE` on `baseline` and `compacted`, plus `storage-summary.txt` | Input rows and bytes, wall time; the Spark medians and the verdict of `baseline_to_compaction` | *Baseline against compaction: same results, measured runtime.* |
| ST06 | Spark UI, Z-order job | The `ZORDER BY (merchant_id)` OPTIMIZE job | *Z-order executed on the isolated `optimized` arm.* |
| ST07 | DBeaver `EXPLAIN ANALYZE` on `compacted` and `optimized`, plus the summary | Physical input of the merchant lookup; the verdict of `compaction_to_zorder` | *Incremental effect of Z-order over compaction.* |
| ST08 | Terminal: `cat "$OUT/storage-summary.txt"` | `equivalence=True`, `rewrite` lines (files removed/added, bytes, seconds), `recommendation` | *Semantic equivalence and rewrite cost from the benchmark's own evidence.* |
| ST09 | DBeaver, the three `ST09` blocks | Splits and physical input bytes: `pruned` and `stats_only` read 7 days, `full_scan` reads all | *Partition pruning against statistics-only skipping against a full scan on the unchanged baseline.* |

**If Trino cannot read a shallow clone** (an error naming an absolute data path): record the error in `$OUT`, and take ST01/ST02/ST04 from `$OUT/storage-evidence/evidence/layout_*.json` and the Spark UI instead. Never register different tables under these names.

Stop Trino afterwards: `docker compose -f infra/docker/schema-viz/compose.yml stop`.

## Stage 5 — Evidence integration (after the screenshots exist)

Hand over:
- the screenshot file names in `~/Pictures/Screenshots/`, with the checkpoint ID of each;
- the `$OUT` folder (it holds no secret except `wh-password`, which is never copied).

Claude then does the following, never before the files exist:
1. Checks each image against its checkpoint.
2. Copies the originals unchanged to `docs/evidence/edai1/docker/5gb/` and `docs/evidence/edai1/storage/5gb/`, with SHA-256 equal to the originals, and adds a `MANIFEST.md` and `SHA256SUMS`.
3. Copies `result.json`, `summary.txt`, the warehouse `provenance.json`, `report.json`, `dbeaver_session.sql`, the storage `plan.json`, `layout_before.json` and `evidence/` next to them.
4. Fills the PENDING tables in both reports with those values.
5. Moves the rubric rows to OWNER_SCREENSHOT_CAPTURED only where the images support them.
