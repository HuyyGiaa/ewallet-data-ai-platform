# EDAI-1 Data Governance and Schema Design: owner evidence runbook

This runbook is for the owner, run on Ubuntu from the repository root on `main`. It collects the runtime evidence for:
- [DATAHUB_GOVERNANCE.md](../../edai1/datahub/DATAHUB_GOVERNANCE.md): rows 28–33, checkpoints DG01–DG08;
- [SCHEMA_DESIGN.md](../../edai1/schema/SCHEMA_DESIGN.md): rows 34–38, checkpoints SC01–SC08.

**Safety.**
- Every cloud step reads only: GCS object listings, `_delta_log` JSON and Parquet footers, and Trino over registered tables. Nothing writes to GCS, and no Airflow, Dataproc or GKE action is involved.
- The only writes go to the local DataHub, Stage 3. They are idempotent and happen only when `--publish` is given.
- Screenshots go to `~/Pictures/Screenshots/`.

| Stage | What | Machine resources | Approx. time |
|---|---|---|---|
| 1 | Metadata readback of the 5GB campaign | network only | 2–15 min (gsutil) |
| 2 | Render and import DBML; SC02–SC04 | browser | 15 min |
| 3 | DataHub publish, readback, DG01–DG08 | about 8 GiB RAM | 40 min |
| 4 | Trino schema queries; SC01, SC05–SC08 | about 4 GiB RAM | 20 min |
| 5 | Evidence integration (after the screenshots exist) | — | — |

Run Stage 3 and Stage 4 one after the other, never at the same time. DataHub Quickstart and the schema-viz Trino together, plus VS Code and kind, have overflowed this laptop's RAM before. Check `free -g` before each stage.

## 0. Common setup (every shell)

```bash
cd ~/Develop/ewallet-data-ai-platform
git status --short --branch          # expect: ## main...origin/main, then only the changes under review
export UV=uv                         # uv 0.13.5; for example the fintech env's uv
export PYTHONPATH=$PWD
export E=docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456
export B=ab-20261110225456 DS=rehearsal-5gb-v1 P=optimized
export SRC_SHA=ac2dfb0eac16e173261b3c3f65e83a03d388528538daefd9ec669bef7123eb80
export OUT=$HOME/ewallet-evidence/edai1-gov-schema-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$OUT"
# Keep virtual environments outside the checkout (an in-tree .venv breaks two CI tests).
export VENVS=$HOME/.cache/ewallet-venvs
gen() { UV_PROJECT_ENVIRONMENT=$VENVS/generation $UV run --locked --project data_platform/generation "$@"; }
dh()  { UV_PROJECT_ENVIRONMENT=$VENVS/datahub    $UV run --locked --project data_platform/metadata/datahub "$@"; }
$UV lock --check --project data_platform/generation && $UV lock --check --project data_platform/metadata/datahub
```

**Profile.** Everything uses `optimized`. Its outputs are semantically equivalent to `baseline` (`comparison.json`), and one profile per DataFlow avoids overwriting job inputs and outputs (DATAHUB_GOVERNANCE §5).

Offline gate before any runtime step. Expect all PASS. One test in `test_schema_diagrams.py` skips until Stage 2.

```bash
gen python -m pytest -q tests/unit/test_schema_diagrams.py tests/unit/test_schema_inventory.py
dh  python -m pytest -q tests/unit/test_batch_governance.py tests/unit/test_datahub_result_assertions.py
```

## Stage 1 — Metadata readback (row 34 input)

**Preconditions:**
- `gcloud auth list` shows your account.
- `gsutil ls gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data/source/rehearsal-5gb-v1/_control/` lists `dataset_manifest.json`.
- If each gsutil call stalls for about 12 s (the IPv6 black hole on this laptop), use the IPv4 shim described in the local notes. Without it the export only takes longer.

```bash
gen python -m data_platform.schema.export_observed_schemas \
  --manifest-dir $E/manifests/$P --benchmark-id $B --spark-profile $P --dataset-version $DS \
  --source-manifest-sha256 $SRC_SHA --comparison $E/comparison.json \
  --output data_platform/schema/generated/observed_schemas.json | tee "$OUT/01_export.json"
```

**Expected output:** `{"source_physical": 10, "bronze": 9, "silver": 9, "gold": 12, "features": 4, ...}`.

**PASS** when all of the following hold:
- the command exits 0 and prints these counts;
- `jq .provenance data_platform/schema/generated/observed_schemas.json` shows `benchmark_id` `ab-20261110225456`, `spark_profile` `optimized` and the source SHA above;
- `jq '[.zones[].tables[] | .commit_count] | max'` is a small integer, normally 1.

**FAIL and what it means:**

| Message | Meaning | Action |
|---|---|---|
| `source manifest SHA-256 … is not the campaign's` | The cloud manifest changed | Stop. Do not continue |
| `inventory mismatch: missing=[…]` | A run root lost a table | Stop and report it |
| `columns differ from the comparison` | The export read another run | Check `$P` and the manifest directory |
| `cannot read` / `failed` | Access or network problem | Re-run after `gcloud auth application-default login`. The command is idempotent |

Optional proof that nothing was written. Run `infra/docker/schema-viz/gcs_log_snapshot.sh > "$OUT/gcs_before.txt"` now, and repeat it after Stage 4 into `gcs_after.txt`.

## Stage 2 — DBML and ERDs (rows 34, 37)

```bash
gen python -m data_platform.schema.render_schema_diagrams | tee "$OUT/02_render.json"
gen python -m data_platform.schema.render_schema_diagrams --check        # exit 0 = committed files current
gen python -m pytest -q tests/unit/test_schema_diagrams.py     # the committed-files test now runs instead of skipping
```

**PASS:** the renderer writes `silver.dbml`, `gold.dbml`, `SCHEMA_INVENTORY.md` and three `trino_*.sql` files with no error, and the tests pass.

**FAIL:** a `schema inputs are inconsistent: …` message names the key, relationship, rule or type that disagrees. Do not edit the generated files by hand. Report the message.

**dbdiagram.io import:**
1. Open https://dbdiagram.io/d and create a new diagram.
2. Replace the editor text with `data_platform/schema/generated/silver.dbml`, for example with `xclip -selection clipboard < data_platform/schema/generated/silver.dbml` and then paste.
3. **Syntax check (authoritative):** the editor's error panel must stay empty. If it shows an error, stop and report the line. Do not fix it in the browser.
4. Use *Auto-arrange*, then drag the three TableGroups (`silver_master_data`, `silver_events`, `silver_labels_and_history`) apart so every Ref line is visible.
5. Capture **SC02**.
6. Repeat with `gold.dbml`, in a new diagram. Place `gold_facts` in the centre, `gold_dimensions` around it, and `historical_features` and `gold_obt_and_aggregates` on the sides. Capture **SC03**.
7. Zoom on `fact_transactions`, `fact_login_events`, `fact_balance_snapshot`, the six dimensions and `dim_merchant_scd2`. Click `fact_transactions.merchant_id` to show its note. Capture **SC04**.

| ID | Rubric | Expected in the image | PASS | Caption (suggested) |
|---|---|---|---|---|
| SC02 | 34, 37 | 9 Silver tables in 3 groups, 12 Ref lines, project note naming `ab-20261110225456` / `optimized` | All 9 tables readable, no error panel | *Silver logical ERD of `rehearsal-5gb-v1` (benchmark `ab-20261110225456`, optimized run `…silver-r261110225457`): 9 tables, 12 references, each checked by a validator rule that passed.* |
| SC03 | 34, 37 | 16 tables (12 core, 4 features) in 5 groups, 21 Ref lines | All table names readable | *Gold fact constellation: 3 facts share 6 dimensions; `dim_merchant_scd2` versions `dim_merchant`; 4 point-in-time feature tables reference their dimensions.* |
| SC04 | 37 | Facts, dimensions, many-to-one markers, optional notes | Line endpoints are on the key columns | *Fact-to-dimension joins on whole dimension keys; `merchant_id` and `counterparty_account_id` are optional references.* |

If one capture is too dense to read, take it in two halves and number them `SC03a` and `SC03b`.

## Stage 3 — DataHub (rows 28–33)

**Preconditions:**
- `free -g` shows at least 8 GiB available.
- The schema-viz Trino is down: `docker ps --filter name=edai1-schema-viz` prints nothing.

Start DataHub:

```bash
UV_PROJECT_ENVIRONMENT=$VENVS/datahub $UV sync --locked --project data_platform/metadata/datahub
export DATAHUB_PYTHON=$VENVS/datahub/bin/python
./data_platform/metadata/datahub/runtime/start_datahub.sh start
./data_platform/metadata/datahub/runtime/start_datahub.sh check          # repeat until it passes
export GMS=http://localhost:8080
curl -fsS $GMS/health && echo GMS-OK
```

Leave `DATAHUB_GMS_TOKEN` unset when local authentication is off. Otherwise export it in the shell, never in the command line. **Troubleshooting:** a GMS that is not ready gives `Connection refused` or HTTP 503; wait and repeat `check`.

### 3.1 Lineage: plan, inspect, publish, verify

```bash
LIN="dh python -m data_platform.metadata.datahub.lineage.batch_lineage --manifest-dir $E/manifests/$P \
  --reports-dir $E/validation_reports --benchmark-id $B --spark-profile $P --dataset-version $DS \
  --source-manifest-sha256 $SRC_SHA --observed-schemas data_platform/schema/generated/observed_schemas.json"
$LIN --output "$OUT/03_lineage_dry_run.json"
jq '.counts, .schema_source, .record_count' "$OUT/03_lineage_dry_run.json"
```

**Expected:** counts `44 / 3 / 8 / 34`, `schema_source` `observed`, `record_count` 141. Without Stage 1 the count is 97 and `schema_source` is pending. Do not publish in that state.

Inspect one record before publishing:

```bash
jq '.records[] | select(.urn|test("dim_merchant_scd2")) | select(.aspect=="upstreamLineage")' "$OUT/03_lineage_dry_run.json"
```

It must name only the Silver `merchant_history` of run `…-optimized-silver-r261110225457`.

Publish:

```bash
$LIN --output "$OUT/04_lineage_publish.json" --publish --gms-url $GMS
jq '.published, ([.verified[] | select(.readback != "MATCH")] | length)' "$OUT/04_lineage_publish.json"
```

**PASS:** `141` and `0`, and the exit code is 0. Running the command again gives the same result, because publication is idempotent.

**FAIL:** `semantic readback mismatch urn=… aspect=…` means DataHub stored something different. The command stops at the first mismatch. Re-run once, because GMS indexing can lag. If it repeats, stop and report the urn and aspect.

### 3.2 Assertions: artifact, dry run, publish, verify

```bash
dh python -m data_platform.metadata.datahub.assertions.campaign_validation \
  --manifest-dir $E/manifests/$P --reports-dir $E/validation_reports --benchmark-id $B \
  --spark-profile $P --dataset-version $DS --source-manifest-sha256 $SRC_SHA \
  --output "$OUT/05_validation_artifact.json"
```

**Expected output:** `outcome_count` 263, `per_status` `{"PASS": 263}`, and `per_pipeline` dp1 54, dp2 153, dp3 56.

```bash
dh python -m data_platform.metadata.datahub.assertions.result_bound_publisher \
  --validation-artifact "$OUT/05_validation_artifact.json" --output "$OUT/06_assertions_dry_run.json" --dry-run
jq '(.runs|length), (.not_published|length)' "$OUT/06_assertions_dry_run.json"    # 263, 0

dh python -m data_platform.metadata.datahub.assertions.result_bound_publisher \
  --validation-artifact "$OUT/05_validation_artifact.json" --output "$OUT/07_assertions_publish.json" --publish \
  --gms-url $GMS
jq '.published_definition_count, .published_run_event_count, (.skipped|length)' "$OUT/07_assertions_publish.json"
```

**PASS:** `263`, `263` and `0`. The output holds the raw definition and run-history API responses for each assertion; keep the file.

**FAIL:** `readback mismatch for …` or `assertionRunEvent missing` means GMS did not index the write in about 10 s. Re-run; the write is idempotent. Never edit the artifact to make a check pass.

Optional smaller first run: add `--stage gold` to the artifact command. It produces 93 outcomes and gives DG06 alone in about a minute.

### 3.3 DataHub screenshots

Open http://127.0.0.1:9002. To find an entity, search its display name; for a dataset, check that the URN in the *Properties* tab contains `-optimized-`.

Each capture must show the entity name, the run identity and the state. For lineage, show the direction and the DataJob nodes where the UI shows them. For assertions, show the name, the dataset, the result and the time.

| ID | Rubric | Navigation | PASS criterion | Provenance to show | Caption (suggested) |
|---|---|---|---|---|---|
| DG01 | 28 | Search `dp1_source_to_bronze` → DataFlow → task `bronze.submit_transformation` → *Lineage*. A second capture of *Properties* is optional | 10 Source inputs (including `transactions_v1`/`v2` and `merchant_history`) → job → 9 Bronze outputs | `dag_run_id` `ab-20261110225456-optimized-dp1`, `dataproc_batch_id` | *DP1 lineage: the Airflow task `bronze.submit_transformation` of DagRun `…-optimized-dp1` reads the 10 Source datasets of `rehearsal-5gb-v1` and writes the 9 Bronze Delta tables.* |
| DG02 | 29 | Dataset `bronze_zone.transactions` → *Quality* → *Assertions* | 6 assertions, all with a passing last run | `source_batch_id` `v1-…-optimized-bronze-…` in a run's details | *DP1 validation: 6 executed rules on Bronze `transactions` (row count, schema, channel, scan) passed in validation batch `v1-…bronze…`.* |
| DG03 | 30 | Dataset `silver_zone.transactions` → *Lineage*, 1 hop upstream | `bronze_zone.transactions` → `silver.submit_transformation` → Silver | DataJob `dag_run_id` `…-optimized-dp2` | *DP2 Bronze→Silver lineage produced by `dp2_bronze_to_curated` task `silver.submit_transformation`.* |
| DG04 | 30 | Dataset `gold_zone.dim_merchant_scd2` → *Lineage*, 3 hops upstream. Then `gold_zone.fact_transactions` | Only `silver_zone.merchant_history` upstream of SCD2; the chain continues to Bronze and Source | run ids in the URNs | *DP2 Silver→Gold lineage: the SCD2 dimension is built from Silver `merchant_history` only, by task `gold.submit_transformation`.* |
| DG05 | 31 | Dataset `silver_zone.transactions` → *Quality* | 17 assertions passing | `source_batch_id` `v1-…silver…` | *DP2 Silver validation: 17 executed rules (keys, domains, conditional requirements, 5 foreign keys) passed.* |
| DG06 | 31 | Dataset `gold_zone.dim_merchant_scd2` → *Quality*. Open `…contiguous_non_overlapping_intervals` | 14 assertions passing; run detail shows `observed` `0` | `source_batch_id` `v1-20261110225456-optimized-gold-r261110225458`, `source_artifact` `…validation-core-gold-a1.json` | *DP2 Gold validation: 14 executed SCD2 rules on `dim_merchant_scd2` passed, including non-overlapping intervals and one current version per merchant.* |
| DG07 | 32 | Search `dp3_historical_features` → task `features.submit_transformation` → *Lineage* | `silver_zone.transactions` and `silver_zone.devices` → job → 4 `feat_*_behavior` | `dag_run_id` `…-optimized-dp3` | *DP3 lineage: point-in-time feature tables are computed from Silver transactions and devices only.* |
| DG08 | 33 | Dataset `gold_zone.feat_user_behavior` → *Quality* | 14 assertions passing, including `pit_user_tx_count_1h_matches_recomputation` and `dp3_features.created_shared_by_run` | `source_batch_id` `v1-…feat…` | *DP3 validation: 14 executed rules including a strict `[T−1h, T)` recomputation and the shared materialization time passed.* |

**Troubleshooting:**
- **The Quality tab shows assertions without results:** the run events are missing. Re-run the 3.2 publish command.
- **The lineage graph shows only datasets:** expand *Show tasks* or open the DataJob page directly.
- **The UI stays on loading skeletons and `/api/graphql` logs `Async request timeout`, while `{ __typename }` still answers:** GMS can no longer get responses from OpenSearch. Seen on 2026-11-13 after the disk passed the 95 % flood stage:
  - OpenSearch rejected bulk writes, and GMS 1.7.0.1 requeued them from its only two OpenSearch I/O threads, which then blocked for good.
  - A thread dump (`docker exec datahub-datahub-gms-quickstart-1 kill -3 1`, then `docker logs`) shows both `I/O dispatcher` threads in `BulkProcessor.internalAdd`.
  - REST reads (`/openapi/v3/entity/...`) still work, because they read MySQL, and no metadata is lost.
  - Fix, in this order:
    1. Free disk until `df -h /` is below 85 %. Docker build cache and images only; never volumes.
    2. Confirm OpenSearch released the blocks: `docker exec datahub-opensearch-1 curl -s localhost:9200/_cluster/settings` must not list `cluster.blocks.create_index`. If it does, clear it with `curl -XPUT -H 'Content-Type: application/json' localhost:9200/_cluster/settings -d '{"persistent":{"cluster.blocks.create_index":null}}'` inside the same container.
    3. Only then run `docker restart datahub-datahub-gms-quickstart-1` once. A restart while OpenSearch still blocks writes gets stuck in the same state.
  - Do not republish to repair this; the metadata in MySQL is intact.
- **A 100GB entity appears in a search:** the old local DataHub volume still holds the historical publication. Filter by the URN that contains `rehearsal-5gb-data`. Never delete volumes to hide it.

Stop DataHub afterwards with `./data_platform/metadata/datahub/runtime/start_datahub.sh stop`. This keeps its volumes.

## Stage 4 — Schema queries (rows 34–38)

**Preconditions:**
- DataHub is stopped and `free -g` shows at least 4 GiB available.
- Port 8091 is free: `ss -ltn | grep ':8091 '` prints nothing.
- The ADC file exists: `gcloud auth application-default print-access-token >/dev/null`.

**Isolated 5GB metastore.** The registrations live in the Docker volume `edai1-schema-viz_metastore_5gb`. The override [`compose.metastore-5gb.yml`](../../../infra/docker/schema-viz/compose.metastore-5gb.yml) mounts that volume instead of the default `edai1-schema-viz_metastore`. The default volume holds the historical 100GB registrations and the Docker/Storage `storage_bench` schema, and must stay unchanged. Both volumes share the container name and port 8091; the one mounted is the one Trino and DBeaver see.

```bash
export EDAI1_GCP_ADC_FILE=$HOME/.config/gcloud/application_default_credentials.json
export SV="-f infra/docker/schema-viz/compose.yml -f infra/docker/schema-viz/compose.metastore-5gb.yml"
docker compose $SV up -d
until [ "$(docker inspect -f '{{.State.Health.Status}}' edai1-schema-viz-trino)" = healthy ]; do sleep 5; done
docker inspect -f '{{range .Mounts}}{{if eq .Destination "/home/trino"}}{{.Name}}{{end}}{{end}}' edai1-schema-viz-trino   # edai1-schema-viz_metastore_5gb
# Register only while the 5GB metastore is empty (first use). A second run fails on the first existing table.
docker exec edai1-schema-viz-trino trino --user viewer --execute "SHOW SCHEMAS FROM delta"   # only information_schema = empty
infra/docker/schema-viz/run_sql.sh registrar data_platform/schema/generated/trino_register_tables.sql > "$OUT/08_register.txt"
grep -c '^CALL' "$OUT/08_register.txt"; grep -c '^CREATE TABLE' "$OUT/08_register.txt"   # 34 and 10
infra/docker/schema-viz/run_sql.sh viewer data_platform/schema/generated/trino_metadata_queries.sql > "$OUT/09_metadata.txt"
infra/docker/schema-viz/run_sql.sh registrar data_platform/schema/generated/trino_provenance_queries.sql > "$OUT/10_provenance.txt"
infra/docker/schema-viz/run_sql.sh viewer data_platform/schema/sql/schema_evidence_queries.sql > "$OUT/11_scd2_features.txt"
```

For the screenshots, run each query in a terminal (`docker exec -it edai1-schema-viz-trino trino --user viewer`) or in any SQL client on `localhost:8091` as user `viewer`, and capture the query together with its result.

| ID | Rubric | Query | PASS | Caption (suggested) |
|---|---|---|---|---|
| SC01 | 34 | SC01 block of `trino_metadata_queries.sql` | `source_zone` 10, `bronze_zone` 9, `silver_zone` 9, `gold_zone` 16 | *All zones of `rehearsal-5gb-v1`, registered read-only from the campaign's run roots: 10 Source datasets, 9 Bronze, 9 Silver and 16 Gold tables.* |
| SC05 | 35 | `DESCRIBE delta.gold_zone.dim_merchant_scd2` | `merchant_sk`, `merchant_id`, `merchant_name`, `category`, `valid_from_ts`, `valid_to_ts`, `is_current` | *Physical schema of the SCD2 merchant dimension.* |
| SC06 | 35 | SC06.1, .3, .4, .5, .8 of `schema_evidence_queries.sql` (two captures if needed) | versions 78294; one merchant's ordered versions with contiguous intervals; 0 violations; as-of rows with one version each | *78,294 merchant versions; the history of one merchant with half-open validity intervals; zero overlap or current-flag violations; transactions resolved to the version valid at their time.* |
| SC07 | 36 | SC07 block of the metadata queries and SC07.1–.3 of the evidence queries | `timestamp` types; 0 nulls; 1 `created` per table; 1 shared across DP3; 0 `created_timestamp` mismatches | *`event_timestamp` (event time) and `created` (materialization time) on the five feature tables.* |
| SC08 | 38 | SC08 block of the metadata queries | 34 rows; Bronze/Silver `zone-qualified business name`; every Gold row has a role, none `unclassified` | *Naming convention: zone-qualified business names in Bronze and Silver, role prefixes in Gold.* |

Provenance for every SC capture: `10_provenance.txt` shows each table's `gs://…rehearsal-5gb-data/lakehouse/runs/ab-20261110225456-optimized-…` location.

Close the stage:

```bash
infra/docker/schema-viz/gcs_log_snapshot.sh > "$OUT/gcs_after.txt"
diff "$OUT/gcs_before.txt" "$OUT/gcs_after.txt" && echo "GCS unchanged"
docker compose $SV stop      # keeps both metastore volumes
```

Never run `docker compose … down -v` on this stack. It deletes the volume of whichever metastore the command names, including the 5GB registrations or the historical `storage_bench` schema. To return to the historical metastore, run `docker compose -f infra/docker/schema-viz/compose.yml up -d` without the override. In DBeaver, press F5 after a switch.

**Troubleshooting:**
- **`Access Denied` on a SELECT or `SHOW CREATE TABLE`:** `viewer` may read but not show DDL. Run SELECTs as `viewer` and the provenance file as `registrar`.
- **Tables of `canonical-100gb-v1` or 8/8/15 tables per zone:** the default metastore is mounted. Start the stack with `$SV`.
- **A table is not found in the 5GB metastore:** register only that table with its line from `trino_register_tables.sql`, run as `registrar`. Do not delete the volume.
- **Slow queries:** SC07.1 reads two columns of about 18.6M rows per table, so it takes a few minutes.

## Stage 5 — Evidence integration (after the screenshots exist)

Hand over:
- the screenshot file names in `~/Pictures/Screenshots/`, with the checkpoint ID of each;
- the `$OUT` folder.

Claude then does the following, never before the files exist:
1. Opens each image and checks that it shows what its checkpoint requires.
2. Copies the originals unchanged to `docs/evidence/edai1/datahub/rehearsal-5gb/` (DG) and `docs/evidence/edai1/schema/rehearsal-5gb/` (SC), naming each copy by its checkpoint ID.
3. Records the SHA-256 of each copy equal to its original in a `MANIFEST.md` and `SHA256SUMS`.
4. Copies the `$OUT` JSON outputs next to them, without credentials or tokens.
5. Embeds each image under its checkpoint in the two reports, with the caption.
6. Moves the rubric rows to DATAHUB_READBACK_VERIFIED, OWNER_SCREENSHOT_CAPTURED and DOCUMENTED only where the image and the readback output support it.

Commit the generated `data_platform/schema/generated/*` from Stage 2 together with the screenshots.
