# Schema Visualization with DBeaver and Trino

This runbook opens the canonical cloud datasets (Source, Bronze, Silver and Gold) in DBeaver through a local, read-only Trino. It then guides the capture of the screenshots for EDAI-1 rubric rows 34 (tables on all zones), 37 (dimension–fact relationships) and 38 (naming conventions). The design and the results are explained in [SCHEMA_DESIGN.md](../../edai1/schema/SCHEMA_DESIGN.md) and [ALL_ZONES_SCHEMA.md](../../edai1/schema/ALL_ZONES_SCHEMA.md).

```text
DBeaver ──JDBC──► Trino 483 (container edai1-schema-viz-trino, 127.0.0.1:8091)
                    ├── catalog delta  ── Delta Lake connector ──► GCS lakehouse/runs/<canonical run>/…
                    ├── catalog hive   ── Hive connector (external Parquet) ──► GCS source/canonical-100gb-v1/…
                    └── local file metastore (Docker volume): table registrations only
```

Nothing in this procedure writes to GCS, changes IAM, starts cloud resources or touches the existing MinIO Trino (`infra/docker/compose.query.yml`, port 8081).

## A. Prerequisites

| Item | Check | Expected |
|---|---|---|
| Repository | `git -C <checkout> log -1 --format=%H` | a commit that contains `infra/docker/schema-viz/` |
| Docker | `docker info --format '{{.ServerVersion}}'` | a version string |
| Trino image | `docker image ls --digests trinodb/trino` | digest `sha256:db58cc93…` (already cached; otherwise `compose up` pulls it) |
| Free memory | `free -g` | at least 4 GiB available. Do not run this alongside the DataHub quickstart |
| Port | `ss -ltn \| grep ':8091 '` | no output |
| GCP identity | `gcloud auth application-default print-access-token > /dev/null && echo ADC-OK` | `ADC-OK` |
| Read access | `gcloud storage ls gs://ewallet-data-ai-platform-hg26-data/source/canonical-100gb-v1/_control/` | `dataset_manifest.json`, `plan.json` |
| DBeaver | *Help → About DBeaver* | CE 26.1.3 was used to prepare these steps; menu labels below are taken from that build |

**Credentials.** Trino reads GCS with your Application Default Credentials file, mounted read-only into the container. The file never enters the repository and is not printed by any command here. The identity only needs `storage.objects.get` and `storage.objects.list` on the data bucket (for example `roles/storage.objectViewer`).

Your current identity has more rights than that. Write protection therefore comes from Trino's access rules (section B.2), not from IAM. Changing IAM is out of scope.

All commands below run from the repository root.

## B. Start the read-only Trino

### B.1 Start and register the tables

```bash
export EDAI1_GCP_ADC_FILE="$HOME/.config/gcloud/application_default_credentials.json"
EVID="$HOME/ewallet-evidence/edai1-schema-viz-$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$EVID"

# 1. Snapshot the cloud objects Trino will read (list-only).
infra/docker/schema-viz/gcs_log_snapshot.sh > "$EVID/gcs_before.txt"

# 2. Start the task-owned Trino.
docker compose -f infra/docker/schema-viz/compose.yml up -d
until [ "$(docker inspect -f '{{.State.Health.Status}}' edai1-schema-viz-trino)" = healthy ]; do sleep 5; done

# 3. Register the canonical runs (local metastore only; 1–2 minutes).
infra/docker/schema-viz/run_sql.sh registrar docs/edai1/schema/generated/trino_register_tables.sql \
  > "$EVID/register.txt"
grep -c '^CALL' "$EVID/register.txt"           # expect 31
grep -c '^CREATE TABLE' "$EVID/register.txt"   # expect 9
```

Registration is needed once per metastore volume. A second run fails with "table already exists". To start over, use `docker compose -f infra/docker/schema-viz/compose.yml down -v`, then repeat step 2.

### B.2 Confirm the access rules

[`rules.json`](../../../infra/docker/schema-viz/rules.json) defines two users. Neither has a password; the endpoint listens on 127.0.0.1 only.

| User | Used by | May |
|---|---|---|
| `viewer` | DBeaver and every query in this runbook | read metadata and data |
| `registrar` | step B.1.3 and the provenance query in section H only | create schemas and tables in the local metastore, and call `delta.system.register_table`. No other procedure is allowed |

Probe the guard with statements that have no target:

```bash
docker exec edai1-schema-viz-trino trino --user viewer --execute "CREATE SCHEMA delta.probe"
# expect: Access Denied: Cannot create schema delta.probe
```

Do not probe with `DROP`, `INSERT`, `DELETE`, `OPTIMIZE` or `VACUUM` against a registered table. If the guard were misconfigured, such a probe would act on the cloud table.

### B.3 Read the metadata from the terminal (raw evidence)

```bash
infra/docker/schema-viz/run_sql.sh viewer docs/edai1/schema/generated/trino_metadata_queries.sql \
  > "$EVID/metadata_readback.txt"
infra/docker/schema-viz/run_sql.sh registrar docs/edai1/schema/generated/trino_provenance_queries.sql \
  > "$EVID/provenance.txt"
```

Both files are metadata only. The queries read `information_schema`, `DESCRIBE`, `SHOW CREATE TABLE` and the Delta `$history` tables. They never scan data files, and they take about one minute. A reference run is committed as [`trino_metadata_readback_20261108T171351Z.txt`](../../evidence/edai1/schema/trino_metadata_readback_20261108T171351Z.txt).

## C. Connect DBeaver

1. **Database → New Database Connection**, search for **Trino**, select it, then **Next**. Accept the driver download if DBeaver asks; it fetches the Trino JDBC driver from Maven.
2. On the **Main** tab:

   | Field | Value |
   |---|---|
   | Host | `localhost` |
   | Port | `8091` |
   | Database / Catalog | `delta` (optional; other catalogs stay visible) |
   | Authentication | Database Native |
   | Username | `viewer` |
   | Password | empty |

   The JDBC URL becomes `jdbc:trino://localhost:8091/delta`.
3. **SSL.** Leave it off. The endpoint is plain HTTP bound to 127.0.0.1, and Trino is configured without TLS or passwords. No certificate check is being bypassed. Do not expose port 8091 beyond loopback.
4. **Connection details → General**: set the name to `EDAI1 cloud schema (Trino 8091, read-only)` and tick **Read-only connection**.
5. **Test Connection** → *Connected*. **Finish**.

> Your workspace already holds a connection named `delta` to `localhost:8081`. That is the **local MinIO** Trino, not the cloud data. Keep the two apart, and capture evidence only from the 8091 connection.

Verify in a SQL editor on the new connection (Ctrl+]):

```sql
SELECT current_user;   -- viewer
SHOW CATALOGS;         -- delta, hive, system
```

**Navigator layout:** `EDAI1 cloud schema` → `delta` → `bronze_zone` / `silver_zone` / `gold_zone` → *Tables*, and `hive` → `source_zone` → *Tables*.

**Do not open the Data tab on large tables.** It runs `SELECT *`, which reads Parquet from GCS. Everything below uses the **Properties → Columns** tab, the **Virtual** tab and the ER diagram, which read metadata only.

## D. Source zone (E01)

The Source zone is Parquet, not Delta. It appears as nine external tables in `hive.source_zone`, because the logical table `transactions` is stored as two physical datasets, `transactions_v1` (no `channel`) and `transactions_v2`. Bronze unions them.

1. Expand `hive` → `source_zone` → *Tables*, to show 9 tables.
2. Open `transactions_v1`, then **Properties → Columns**. Repeat for `transactions_v2`. The difference is `channel`.
3. Optional SQL: the `E01` block of `trino_metadata_queries.sql`.

Row counts and file counts come from the dataset manifest, not from Trino. They are listed in [ALL_ZONES_SCHEMA.md](../../edai1/schema/ALL_ZONES_SCHEMA.md#source). Do not run `COUNT(*)` on Source tables: each scan reads up to 47 GB.

## E. Bronze zone (E02)

1. Expand `delta` → `bronze_zone` → *Tables*, to show 8 tables.
2. Open `transactions` → **Properties → Columns**: 15 columns including `channel`. Timestamps are `timestamp(6)`, which is Spark `timestamp_ntz`.
3. SQL: the `E02` block.

## F. Silver and Gold zones (E03, E04)

1. `delta` → `silver_zone` → *Tables*: 8 tables. Open `transactions`: 16 columns. It gains the partition column `event_date`, and its timestamps become `timestamp(3) with time zone`.
2. `delta` → `gold_zone` → *Tables*: 15 tables, made of 5 `dim_`, 3 `fact_`, 1 `obt_`, 5 `feat_` and 1 `opt_` table.
3. Open `fact_transactions` → **Properties → Columns**: 17 columns. Its partition column is `event_date`.
4. SQL: the `E03` and `E04` blocks.

## G. Gold dimension–fact ER diagram (E05, E06)

Delta has no primary or foreign keys, so DBeaver first shows eight unconnected boxes. The relationships are added as **virtual keys**. They are stored in your DBeaver workspace only, never in Trino or GCS. The authoritative list is [GOLD_ERD.md](../../edai1/schema/generated/GOLD_ERD.md): 8 virtual unique keys and 13 virtual foreign keys.

### G.1 Virtual unique keys (8)

For each table below: double-click it in the navigator, open the **Virtual** tab, and under **Virtual Unique Keys** choose **Add**/**Edit…**. Tick the key column(s), then **OK**, then **Save** (Ctrl+S).

| Table | Key column(s) |
|---|---|
| `dim_user` | `user_id` |
| `dim_account` | `account_id` |
| `dim_merchant` | `merchant_id` |
| `dim_device` | `device_id` |
| `dim_date` | `date_key` |
| `fact_transactions` | `transaction_id` |
| `fact_login_events` | `login_id` |
| `fact_balance_snapshot` | `account_id`, `snapshot_date` |

### G.2 Virtual foreign keys (13)

On the child table's **Virtual** tab, under **Virtual Foreign Keys**, choose **Add**. Set **Ref Datasource** to this connection and **Ref Table** to the parent table. Pick the parent's virtual unique key, map the column under **Columns**, then **OK** and **Save**.

| # | Child table | Column | Ref Table | Ref column | Optional |
|---:|---|---|---|---|---|
| 1 | `dim_account` | `user_id` | `dim_user` | `user_id` | no |
| 2 | `dim_device` | `user_id` | `dim_user` | `user_id` | no |
| 3 | `fact_transactions` | `user_id` | `dim_user` | `user_id` | no |
| 4 | `fact_transactions` | `account_id` | `dim_account` | `account_id` | no |
| 5 | `fact_transactions` | `device_id` | `dim_device` | `device_id` | no |
| 6 | `fact_transactions` | `merchant_id` | `dim_merchant` | `merchant_id` | **yes** (payments only) |
| 7 | `fact_transactions` | `counterparty_account_id` | `dim_account` | `account_id` | **yes** (transfers only) |
| 8 | `fact_transactions` | `date_key` | `dim_date` | `date_key` | no |
| 9 | `fact_login_events` | `user_id` | `dim_user` | `user_id` | no |
| 10 | `fact_login_events` | `device_id` | `dim_device` | `device_id` | no |
| 11 | `fact_login_events` | `date_key` | `dim_date` | `date_key` | no |
| 12 | `fact_balance_snapshot` | `account_id` | `dim_account` | `account_id` | no |
| 13 | `fact_balance_snapshot` | `date_key` | `dim_date` | `date_key` | no |

`fact_transactions` has two edges to `dim_account`, numbers 4 and 7. Create them as two separate virtual foreign keys. DBeaver cannot express "optional"; that information comes from the table above and from GOLD_ERD.md.

**Verify:** the **Virtual Foreign Keys** list on `fact_transactions` holds 6 rows, on `fact_login_events` 3, on `fact_balance_snapshot` 2, and on `dim_account` and `dim_device` 1 each.

### G.3 Build the diagram

1. **File → New → DBeaver → ER Diagram**. In the wizard (*Create new diagram*), name it `edai1_gold_dim_fact`. Under *Manage diagram content*, expand the connection to `delta` → `gold_zone` and tick the 8 tables. Alternatively, on `gold_zone` use **View Diagram**, then *Remove from diagram* every table that is not one of the 8.
2. Use **Arrange Diagram**, then drag the boxes: `fact_transactions` in the centre, the five dimensions around it, `fact_login_events` and `fact_balance_snapshot` on the sides. Turn on **Keep layout** so the arrangement persists.
3. Right-click the canvas: **Notation → Crow's Foot**. Under **Show Attributes**, choose all columns for E06, or keys only for a compact E05.
4. Check that 13 links are drawn and that `fact_transactions` has 6.
5. **Export:** right-click → **Save diagram as …** → PNG, as `E05_gold_dim_fact_erd.png`. Also save the `.erd` format to `docs/evidence/edai1/schema/edai1_gold_dim_fact.erd`, so the layout can be reopened.
6. **Reproduce later:** the diagram lives in the DBeaver project under *ER diagrams*. After `down -v` and a re-registration (B.1), table identities stay the same (`delta.gold_zone.<table>`), so the virtual keys and the diagram reconnect after **Refresh** (F5) on the connection. If the connection itself was deleted, repeat G.1–G.3.

## H. Naming conventions (E07)

1. In the navigator, expand `bronze_zone`, `silver_zone` and `gold_zone` side by side. Bronze and Silver hold the same eight business names; Gold shows the `dim_`/`fact_`/`obt_`/`feat_`/`opt_` prefixes.
2. Run the `E07` query from `trino_metadata_queries.sql`. Its `naming_role` column classifies every registered table.

## I. Provenance (E08)

`$EVID/provenance.txt` (B.3) shows `location = 'gs://ewallet-data-ai-platform-hg26-data/lakehouse/runs/<canonical run>/…'` for one table per zone, plus `external_location` for Source.

In DBeaver, `SHOW CATALOGS`, together with the connection's `localhost:8091` URL, is the capture.

Finish with the unchanged-cloud check:

```bash
infra/docker/schema-viz/gcs_log_snapshot.sh > "$EVID/gcs_after.txt"
diff "$EVID/gcs_before.txt" "$EVID/gcs_after.txt" && echo "GCS unchanged"
```

## J. Evidence matrix

Save screenshots to `docs/evidence/edai1/screenshots/schema/`. A row stays `OWNER_SCREENSHOT_PENDING` until its file exists; never link a missing image from a report.

| ID | Screen or command | Expected | Screenshot | Raw artifact | Proves | Does not prove |
|---|---|---|---|---|---|---|
| E01 | Navigator `hive.source_zone` + Columns of `transactions_v1`/`_v2` | 9 tables; `channel` only in v2 | `E01_source_tables.png` | readback `E01` block; `observed_schemas.json` → `source` | Source datasets on GCS, read through Trino from their real Parquet files | row counts (from the manifest, not Trino) |
| E02 | Navigator `delta.bronze_zone` + Columns of `transactions` | 8 tables; 15 columns | `E02_bronze_tables.png` | `E02` block | Bronze Delta schemas of the canonical run | data quality |
| E03 | Navigator `delta.silver_zone` + Columns of `transactions` | 8 tables; `event_date` added | `E03_silver_tables.png` | `E03` block | Silver schemas and partition column | deduplication (proved by validators) |
| E04 | Navigator `delta.gold_zone` + Columns of `fact_transactions` | 15 tables; 17 columns | `E04_gold_tables.png` | `E04` block | Gold inventory and fact columns | contents of the tables |
| E05 | ER diagram `edai1_gold_dim_fact`, keys only | 8 tables, 13 links | `E05_gold_dim_fact_erd.png` | `.erd` export; [GOLD_ERD.md](../../edai1/schema/generated/GOLD_ERD.md) | the logical star model on the real tables | physical constraints: the links are DBeaver virtual keys |
| E06 | ER diagram with all attributes, plus the **Virtual** tab of `fact_transactions` | 6 virtual FKs on the fact | `E06_keys_and_relationships.png` | `E06` block (`DESCRIBE`); GOLD_ERD.md | join columns and keys | optionality (documented in GOLD_ERD.md; DBeaver cannot show it) |
| E07 | Three zone nodes expanded; E07 query result | unprefixed Bronze/Silver names; prefixed Gold names | `E07_naming_conventions.png` | `E07` block | the naming rules as stored in the catalog | that every future table will follow them |
| E08 | Connection editor (8091) + `SHOW CATALOGS`; terminal `provenance.txt` | `gs://…/runs/<canonical run>/…` locations | `E08_trino_cloud_provenance.png` | `provenance.txt`; `gcs_before/after.txt` | the screens show the canonical cloud runs, and GCS was not modified | anything about the MinIO stack on 8081 |

## K. Troubleshooting

| Symptom | Cause | Recovery |
|---|---|---|
| `compose up` fails: `set EDAI1_GCP_ADC_FILE` | variable not exported | export it (B.1) in the same shell |
| Container exits; log shows `local.location … does not exist` | edited catalog config | restore the files in `infra/docker/schema-viz/catalog/` |
| `register_table` fails: `Failed checking table location` / 403 | ADC missing, expired or lacks read access | `gcloud auth application-default login`, then re-check A |
| ADC unreadable in the container | host UID is not 1000 (the container's `trino` user) | copy the ADC to a file readable by UID 1000 and point `EDAI1_GCP_ADC_FILE` at it |
| `SHOW TABLES` is empty | fresh volume, nothing registered | run B.1.3 |
| `Table already exists` during registration | volume already registered | nothing to do, or `down -v` and re-register |
| DBeaver shows MinIO tables or other run IDs | connected to `localhost:8081` | use the 8091 connection; verify with `SHOW CREATE TABLE` (B.3) |
| A table is missing | wrong run, or the cloud run was deleted | compare against `observed_schemas.json`; `gcloud storage ls <root>/<table>/_delta_log/` |
| ERD shows no links | no physical keys exist (expected) | add the virtual keys (G.1, G.2) |
| Virtual FK not drawn | parent virtual key missing, or editor not saved | create the parent key first, Ctrl+S, then **Refresh Diagram** |
| DDL tab: `Cannot show create table` | `viewer` lacks table ownership | expected; use `provenance.txt` |
| `Access Denied` for any write | the guard working | none. Do not loosen `rules.json` |

## L. Cleanup

Only stop what this runbook started:

```bash
docker compose -f infra/docker/schema-viz/compose.yml down      # keep registrations
docker compose -f infra/docker/schema-viz/compose.yml down -v   # also remove the local metastore
```

These commands stop and remove only the `edai1-schema-viz` project: the container `edai1-schema-viz-trino`, its network and its volume. They do not stop kind clusters, the MinIO Trino or any other container. DBeaver virtual keys stay in the DBeaver workspace until you delete them on the **Virtual** tab.
