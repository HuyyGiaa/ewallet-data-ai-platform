# DataHub local runtime

DataHub stays separate from the platform Compose project. The helper in this
directory makes the tested Quickstart runtime reproducible without checking a
large upstream Compose file into this repository.

It requires Docker Compose v2, Python 3, `curl`, and `sha256sum`.

Tested versions:

| Component | Version |
|---|---|
| Requested Quickstart series | `v1.7.0` |
| Pinned Quickstart Compose Git ref | `v1.7.0.1` |
| DataHub server image | `v1.7.0.1` |
| `acryl-datahub` CLI/SDK | `1.7.0.5` |

The upstream Quickstart file is downloaded from the pinned `v1.7.0.1` Git ref
and checked against its SHA-256 digest. This is the Compose ref that DataHub
CLI `1.7.0.5` maps from the requested `v1.7.0` series. The helper then changes
exactly two external Kafka
values from port `9092` to `9093`. Internal DataHub traffic remains on
`broker:29092`. Generated Compose files are stored in this directory's ignored
`.cache/` folder.

From the repository root, install the committed DataHub dependency lock and
use that interpreter:

```bash
uv lock --check --project data_platform/metadata/datahub
uv sync --locked --project data_platform/metadata/datahub
export DATAHUB_PYTHON="$PWD/data_platform/metadata/datahub/.venv/bin/python"
./data_platform/metadata/datahub/runtime/start_datahub.sh start
./data_platform/metadata/datahub/runtime/start_datahub.sh check
```

The start command pulls the pinned images by default. When the exact images are
already present, set `DATAHUB_PULL_IMAGES=false` to skip that pull.

Do not install the SDK manually into a running DataHub container or substitute
an unlocked Conda environment when collecting reproducibility evidence.

Stop the containers without deleting the persisted Quickstart volumes:

```bash
./data_platform/metadata/datahub/runtime/start_datahub.sh stop
```

The helper uses the pinned official Compose file directly and never invokes DataHub's destructive `nuke` command and never
removes the `datahub_broker`, `datahub_mysqldata`, or `datahub_osdata` volumes.
