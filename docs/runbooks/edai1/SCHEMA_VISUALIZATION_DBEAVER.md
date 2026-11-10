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

## G. Native ER diagrams: Silver and Gold (E05, E06, E09)

Two native DBeaver diagrams are provided as project files:

| Diagram | Tables | Relationship lines | File in the DBeaver project |
|---|---|---|---|
| `EDAI1_Silver_ERD` | the 8 tables of `delta.silver_zone` | 11 | `General/Diagrams/EDAI1_Silver_ERD.erd` |
| `EDAI1_Gold_ERD` | the 15 tables of `delta.gold_zone` | 19 (13 of them dimension–fact) | `General/Diagrams/EDAI1_Gold_ERD.erd` |

`ops/schema/dbeaver_erd.py` generates both into [docs/edai1/schema/dbeaver/](../../edai1/schema/dbeaver/). Its output uses the `.erd` format of the installed DBeaver 26.1.3 (`DiagramLoader` in the `ui.editors.erd` plugin). Each file names the connection `EDAI1 - GCS Cloud Schema` by its id, plus every table as `delta` → `<schema>` → `<table>`, with its position and the labelled group notes.

**A `.erd` file stores no column-level relationship.** DBeaver draws a line from a table's foreign keys. Delta has none, so the lines come from **virtual keys**: DBeaver stores them in its own workspace configuration, never in Trino or GCS. They are created in the UI (G.2), so DBeaver writes that configuration itself. The script never edits it.

The authoritative list is [DBEAVER_VIRTUAL_KEYS.md](../../edai1/schema/dbeaver/DBEAVER_VIRTUAL_KEYS.md). An edge is listed there only when it is in `schema_relationships.yml` **and** a rule in `validate_silver.py` or `validate_gold.py` checks it for orphans. `obt_transaction_enriched.transaction_id → fact_transactions` is ETL lineage with no validator rule, so it is deliberately left undrawn.

### G.1 Install and open the diagrams

```bash
python3 -m ops.schema.dbeaver_erd install   # creates General/Diagrams/*.erd; never overwrites, never touches .dbeaver/
python3 -m ops.schema.dbeaver_erd check     # PASS = both files present and bound to the 8091 connection
```

1. In DBeaver, open the **Projects** view (**Window → Projects**). Right-click **General → Refresh** (F5), because the files were added outside DBeaver.
2. Expand **General → Diagrams** and double-click `EDAI1_Silver_ERD`. DBeaver connects to `localhost:8091` and draws 8 boxes with their real columns and types, plus two notes. Until G.2 is done there are no lines.
3. Open `EDAI1_Gold_ERD` the same way: 15 boxes in three groups. The OBT/optimization tables are on the left, the dimensional core in the centre, the feature tables on the right.

If a box is missing, the editor shows an error such as *Container 'delta' not found*. Check that the connection is connected and that `SHOW TABLES FROM delta.gold_zone` returns 15 rows (B.3).

### G.2 Create the virtual keys (once)

Virtual keys belong to the tables, not to a diagram, so each key is defined once and appears in every diagram that shows both tables.

**Virtual unique keys (10).** For each parent table: double-click it in the navigator, open the **Virtual** tab, and under **Virtual Unique Keys** choose **Add**. Tick the key column, click **OK**, then **Save** (Ctrl+S).

| Silver table | Key | Gold table | Key |
|---|---|---|---|
| `users` | `user_id` | `dim_user` | `user_id` |
| `accounts` | `account_id` | `dim_account` | `account_id` |
| `merchants` | `merchant_id` | `dim_merchant` | `merchant_id` |
| `devices` | `device_id` | `dim_device` | `device_id` |
| `transactions` | `transaction_id` | `dim_date` | `date_key` |

**Virtual foreign keys (11 Silver + 19 Gold).** For each row of DBEAVER_VIRTUAL_KEYS.md, open the *child* table's **Virtual** tab and choose **Add** under **Virtual Foreign Keys**. Set **Ref Datasource** to this connection and **Ref Table** to the parent, pick the parent's virtual unique key, and map the column under **Columns**. Then click **OK** and **Save**.

You can also draw a key on the open diagram with the palette's connection tool, dragging from the child column to the parent column. DBeaver then creates the same logical virtual foreign key (`AssociationCreateCommand` → `createVirtualForeignKey`) and saves it.

`transactions` / `fact_transactions` reference `accounts` / `dim_account` twice: through `account_id` and through `counterparty_account_id`. Create each as a separate key. DBeaver cannot show "optional", so the optional edges (`merchant_id`, `counterparty_account_id`) are documented in DBEAVER_VIRTUAL_KEYS.md and in the Silver note.

**Verify on each child's Virtual tab:**

| Diagram | Virtual foreign keys per child table |
|---|---|
| Silver (11) | `transactions` 5; `login_events` 2; `accounts`, `devices`, `balance_snapshots`, `fraud_labels` 1 each |
| Gold (19) | `fact_transactions` 6; `fact_login_events` 3; `fact_balance_snapshot` 2; `dim_account`, `dim_device`, `feat_user_90d`, `opt_merchant_performance` and the four `feat_*_behavior` tables 1 each |

### G.3 Persistence check and capture

1. On each open diagram, press F5 (refresh): Silver now shows 11 lines, Gold 19. Optionally set **Notation → Crow's Foot** from the canvas context menu. Tidy any box that DBeaver sized differently from the layout, then press **Ctrl+S** to save the diagram.
2. Close both editors. Reopen them from **General → Diagrams** and confirm the same tables, lines and positions. The editor's connection must be `EDAI1 - GCS Cloud Schema` (`localhost:8091`), never `delta` (8081). Only after this check is diagram persistence verified.
3. Capture (J):
   - **E09:** the whole Silver diagram.
   - **E05:** the whole Gold diagram (zoom to fit).
   - **E06:** zoom the same Gold diagram on the centre group (5 dimensions, 3 facts, 13 lines), plus the **Virtual** tab of `fact_transactions` with its 6 keys.
4. To keep the diagrams as files too, use **right-click → Save diagram as …** in PNG and `.erd` format into `docs/evidence/edai1/screenshots/schema/`. Exports are only made from the live editor.
5. **Reproduce later:** after `down -v` and a re-registration (B.1), table identities stay `delta.<schema>.<table>`. The diagrams and virtual keys therefore reconnect after **Refresh** on the connection. If the connection is deleted, its virtual keys go with it: repeat G.2.

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

E05 and E06 were first defined for an 8-table diagram `edai1_gold_dim_fact`. They now refer to the 15-table `EDAI1_Gold_ERD`, whose centre group is that same 8-table model. E09 (Silver) was appended rather than renumbering E07 and E08.

| ID | Screen or command | Expected | Screenshot | Raw artifact | Proves | Does not prove |
|---|---|---|---|---|---|---|
| E01 | Navigator `hive.source_zone` + Columns of `transactions_v1`/`_v2` | 9 tables; `channel` only in v2 | `E01_source_tables.png` | readback `E01` block; `observed_schemas.json` → `source` | Source datasets on GCS, read through Trino from their real Parquet files | row counts (from the manifest, not Trino) |
| E02 | Navigator `delta.bronze_zone` + Columns of `transactions` | 8 tables; 15 columns | `E02_bronze_tables.png` | `E02` block | Bronze Delta schemas of the canonical run | data quality |
| E03 | Navigator `delta.silver_zone` + Columns of `transactions` | 8 tables; `event_date` added | `E03_silver_tables.png` | `E03` block | Silver schemas and partition column | deduplication (proved by validators) |
| E04 | Navigator `delta.gold_zone` + Columns of `fact_transactions` | 15 tables; 17 columns | `E04_gold_tables.png` | `E04` block | Gold inventory and fact columns | contents of the tables |
| E05 | Native diagram `EDAI1_Gold_ERD`, zoom to fit | 15 tables in 3 groups; 19 lines | `E05_gold_erd_overview.png` | [EDAI1_Gold_ERD.erd](../../edai1/schema/dbeaver/EDAI1_Gold_ERD.erd); [DBEAVER_VIRTUAL_KEYS.md](../../edai1/schema/dbeaver/DBEAVER_VIRTUAL_KEYS.md) | the whole Gold zone on the real tables, and which tables carry validated key references | physical constraints: the lines are DBeaver virtual keys; ETL lineage (OBT and feature derivation) |
| E06 | Same diagram zoomed on the dimensional core, plus the **Virtual** tab of `fact_transactions` | 5 dims, 3 facts, 13 lines; 6 virtual FKs on the fact | `E06_gold_dim_fact_detail.png` | `E06` block (`DESCRIBE`); [GOLD_ERD.md](../../edai1/schema/generated/GOLD_ERD.md) | join columns, keys and the outriggers to `dim_user` | optionality (documented in GOLD_ERD.md; DBeaver cannot show it) |
| E07 | Three zone nodes expanded; E07 query result | unprefixed Bronze/Silver names; prefixed Gold names | `E07_naming_conventions.png` | `E07` block | the naming rules as stored in the catalog | that every future table will follow them |
| E08 | Connection editor (8091) + `SHOW CATALOGS`; terminal `provenance.txt` | `gs://…/runs/<canonical run>/…` locations | `E08_trino_cloud_provenance.png` | `provenance.txt`; `gcs_before/after.txt` | the screens show the canonical cloud runs, and GCS was not modified | anything about the MinIO stack on 8081 |
| E09 | Native diagram `EDAI1_Silver_ERD` | 8 tables; 11 lines | `E09_silver_erd.png` | [EDAI1_Silver_ERD.erd](../../edai1/schema/dbeaver/EDAI1_Silver_ERD.erd); DBEAVER_VIRTUAL_KEYS.md | the relational Silver model on the real tables | physical constraints; that the model is a star schema (it is not) |

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
| ERD shows no links | no physical keys exist (expected) | create the virtual keys (G.2), then F5 on the diagram |
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
