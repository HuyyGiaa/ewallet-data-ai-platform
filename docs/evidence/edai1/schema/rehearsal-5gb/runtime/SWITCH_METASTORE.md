# Schema-viz Trino: switching between the 5GB and the historical metastore

Both metastores use the same container (`edai1-schema-viz-trino`), the same port (`127.0.0.1:8091`) and the same users (`viewer` read-only, `registrar`). Only the volume mounted at `/home/trino` differs.

Whichever one is running is what DBeaver sees on `localhost:8091`.

| Metastore | Docker volume | Contents |
|---|---|---|
| **5GB (current)** | `edai1-schema-viz_metastore_5gb` | 44 tables of `ab-20261110225456-optimized` / `rehearsal-5gb-v1`: `source_zone` 10, `bronze_zone` 9, `silver_zone` 9, `gold_zone` 16 |
| Historical | `edai1-schema-viz_metastore` | 40 tables of the 100GB campaign `ab-20261025134342` / `canonical-100gb-v1`, plus the 6 `storage_bench` tables of the Docker/Storage work |

Switching recreates only the container. Neither volume is deleted or modified by a switch. **Never run `docker compose … down -v`**: it deletes the volume of whichever stack it names.

## Common setup

```bash
cd ~/Develop/ewallet-data-ai-platform
export EDAI1_GCP_ADC_FILE=$HOME/.config/gcloud/application_default_credentials.json
export SV=infra/docker/schema-viz/compose.yml
export SV5=$HOME/ewallet-evidence/edai1-gov-schema-20261113T024024Z/schema_trino_5gb/compose.metastore-5gb.yml
```

## Use the 5GB metastore (Schema Design SC01, SC05–SC08)

```bash
docker compose -f $SV -f $SV5 up -d
until [ "$(docker inspect -f '{{.State.Health.Status}}' edai1-schema-viz-trino)" = healthy ]; do sleep 5; done
docker inspect -f '{{range .Mounts}}{{if eq .Destination "/home/trino"}}{{.Name}}{{end}}{{end}}' edai1-schema-viz-trino
# expect: edai1-schema-viz_metastore_5gb
```

The registrations persist in the volume, so do not register again. If a table is ever missing, run `infra/docker/schema-viz/run_sql.sh registrar docs/edai1/schema/generated/trino_register_tables.sql`. Its schema statements are `IF NOT EXISTS`, but a `register_table` call for an existing table fails and stops the script. Register only the missing table in that case.

## Use the historical metastore (100GB history, `storage_bench`)

```bash
docker compose -f $SV up -d
until [ "$(docker inspect -f '{{.State.Health.Status}}' edai1-schema-viz-trino)" = healthy ]; do sleep 5; done
docker inspect -f '{{range .Mounts}}{{if eq .Destination "/home/trino"}}{{.Name}}{{end}}{{end}}' edai1-schema-viz-trino
# expect: edai1-schema-viz_metastore
```

## Stop (keeps both volumes)

```bash
docker compose -f $SV stop
```

## Quick check of which metastore is live

```bash
docker exec edai1-schema-viz-trino trino --user viewer --execute \
  "SELECT table_schema, count(*) FROM delta.information_schema.tables WHERE table_schema <> 'information_schema' GROUP BY 1 ORDER BY 1"
# 5GB:        bronze_zone 9, gold_zone 16, silver_zone 9
# historical: bronze_zone 8, gold_zone 15, silver_zone 8, storage_bench 6
```

In DBeaver, refresh the connection (F5) after a switch, because it caches the table list.
