# RAG runbook

This runbook runs the RAG ingestion DAG `rag_fraud_policy_ingestion` from a
clean checkout of `main`, verifies the result, and captures the runtime
screenshots. The integration target is the **Hybrid** deployment (§5):
Airflow, the embedding model and DataHub run on the laptop, the Feast registry
and offline Parquet live on GCS, and the Feast Online Store is the Redis on GKE
that the RAG MCP server and the agents use. The local target (§2–§3) is the
development and test profile of the same pipeline. The design is explained in
[RAG_PIPELINE.md](../../../edai2/rag/RAG_PIPELINE.md). Every command runs from
the repository root.

## Safety classes

| Label | Effect |
|---|---|
| `READ-ONLY` | Reads files, Airflow, Feast, Redis or DataHub; changes nothing |
| `LOCAL` | Starts or stops local processes and containers, or writes local files |
| `TARGET-WRITE` | Writes Feast rows and the ownership marker to the selected Redis database, and metadata to DataHub |
| `CLOUD` | Needs or uses the GKE cluster or GCS; only for the `gke` target |
| `COST` | Adds billed cloud capacity (waking the cluster); owner only |

Rules:

- Never run `FLUSHDB` or `FLUSHALL`, and never delete Redis keys.
- Never point a run at Redis DB 0 or DB 1, or at the Feast project
  `ewallet_rag`. The code refuses these targets unless production mode is chosen
  explicitly.
- Every run needs a new `RUN_LABEL`. Artifacts are written once.
- Never print the settings file, and never commit it.

## 0. Prerequisites

| Item | Value |
|---|---|
| uv | 0.13.5 (`~/miniconda3/envs/fintech/bin/uv` on the owner's laptop) |
| Task runtime | Python 3.10, `infra/docker/rag-ingestion` (`pyproject.toml` + `uv.lock`) |
| Airflow | 3.3.1 in the conda env `airflow`. The runner starts its own isolated instance; `~/airflow` is not used |
| DataHub | Local quickstart v1.7.0.1 (`data_platform/metadata/datahub/runtime/start_datahub.sh`), GMS 127.0.0.1:18080, UI http://localhost:9002 |
| Docker | For DataHub and, with the local target, Redis `redis:7.4.2-alpine` |
| Resources | About 6 GiB free RAM for DataHub, at least 10 GiB free disk. Check with `free -g` and `df -h /` |

Ports used:

| Port | Service |
|---|---|
| 8095 | Isolated Airflow UI and API |
| 8798, 8799 | Isolated Airflow log servers |
| 16381 | Local Redis |
| 16380 | Port-forward to the GKE Redis (gke target only) |
| 18080 | DataHub GMS |
| 9002 | DataHub UI |
| 9200 | OpenSearch |

Paths:

| Path | Content |
|---|---|
| `~/.config/ewallet-rag/runtime.local.env` | Settings for the local target, mode 600, written by `prepare`. It contains no credential |
| `~/.config/ewallet-rag/runtime.env` | Settings for the gke target, written by the owner (§5) |
| `~/ewallet-evidence/rag/rag_<label>/` | Run artifacts |
| `~/ewallet-evidence/rag/feast/` | Local Feast registry and offline Parquet |
| `~/.cache/ewallet-rag-airflow/` | Isolated Airflow home, database and task logs |
| `~/.cache/ewallet-rag-hf/` | Embedding model cache |

## 1. Tests (no services)

```bash
# [LOCAL] lock check and tests in the locked runtime
uv lock --check --project infra/docker/rag-ingestion
uv run --locked --project infra/docker/rag-ingestion python -m pytest -q \
  tests/llm_agent/test_rag_airflow_ingestion.py tests/llm_agent/test_rag_online_retriever.py \
  tests/llm_agent/test_rag_chunk_lookup.py tests/llm_agent/test_phase_c.py \
  tests/llm_agent/test_rag_dag_reconciliation.py tests/llm_agent/test_rag_online_target.py \
  tests/llm_agent/test_rag_governance_visibility.py tests/llm_agent/test_phase_abc_p0_fixes.py \
  tests/llm_agent/test_rag_runtime_forward.py
# [LOCAL] real Airflow parse of the DAG in a throwaway home (six tasks, no import errors)
bash scripts/validation/validate_rag_dag_import.sh ~/miniconda3/envs/airflow/bin/python /tmp/rag-dag-parse.json
```

Pass: all tests pass, with one opt-in model test skipped. The parse report
lists only `rag_fraud_policy_ingestion`, with no import errors.

A faster orchestration check uses `airflow dags test` with the SQLite Feast
profile and publishes nothing to DataHub. Its `datahub.json` says
`PLAN_ONLY_NOT_PUBLISHED`, so it is a readiness check, not evidence:

```bash
# [LOCAL]
scripts/validation/run_rag_dag_isolated.sh ~/miniconda3/envs/airflow/bin/airflow dagtest-$(date +%s)
```

## 2. Prepare and start (local target)

```bash
# [LOCAL] sync the locked runtime, cache the model, write the local settings, start Redis on 127.0.0.1:16381
scripts/llm_agent/rag_runtime_run.sh prepare
# [LOCAL] start DataHub (if it is not healthy) and the isolated Airflow on http://127.0.0.1:8095
scripts/llm_agent/rag_runtime_run.sh start
# [READ-ONLY] every line must be PASS
scripts/llm_agent/rag_runtime_run.sh preflight
```

The settings for the local target are:

| Setting | Value |
|---|---|
| Mode | `verification` |
| Feast project | `ewallet_rag_verify` |
| Redis | `127.0.0.1:16381,db=3` |
| Registry and offline Parquet | under `~/ewallet-evidence/rag/feast/ewallet_rag_verify/` |
| DataHub | `http://127.0.0.1:18080` |

The Redis container is ephemeral: `stop` ends it, and its data with it. Take
the Online screenshots before stopping it.

If a preflight line fails:

| FAIL | Fix |
|---|---|
| checkout is clean | Commit or stash; the run records the source commit |
| locked task runtime | `uv sync --locked --no-dev --project infra/docker/rag-ingestion` |
| embedding model cached | Rerun `prepare` (downloads about 90 MB once) |
| Redis database is empty or already owned | Another project owns DB 3. Run `stop` and then `prepare` for a fresh container, or use another database |
| DataHub GMS healthy | Wait one or two minutes after `start`; `./data_platform/metadata/datahub/runtime/start_datahub.sh check` |
| OpenSearch indices writable | Free disk below 90 %, then clear the block: `curl -X PUT localhost:9200/_all/_settings -H 'Content-Type: application/json' -d '{"index.blocks.read_only_allow_delete": null}'` |
| isolated Airflow healthy | `scripts/llm_agent/rag_isolated_airflow.sh status`; the logs are in `~/.cache/ewallet-rag-airflow/logs/*.out` |

## 3. Run and verify

```bash
# [TARGET-WRITE] a new DagRun manual__rag-<label>; waits up to 30 minutes, then collects
LABEL=rag-$(date -u +%Y%m%dt%H%M%S)
scripts/llm_agent/rag_runtime_run.sh trigger "$LABEL"
```

`trigger` exits 0 only if the DagRun succeeds. The run directory
`~/ewallet-evidence/rag/rag_$LABEL/` then contains the following:

| File | Must show |
|---|---|
| `airflow/task_states.json` | 6 tasks `success` |
| `documents.json` | 2 documents, `checksum_mismatches` 0 |
| `chunks.json` | 4 chunks, `deterministic` true |
| `embeddings.json` | 384 dimensions, `invalid_vectors` 0, model revision `1110a243…` |
| `publication.json` | 4 rows; identity `b85ae796…`; `online_target` with `claim_status` `CLAIMED` (first run on the database) or `OWNED` |
| `validation.json` | `readback_status` `MATCH`, `recall_at_k` 1.0, `citation_errors` 0, `online_target.owner` `MATCH` |
| `datahub.json` | `PUBLISHED_AND_READ_BACK`, 27 aspects read back, 5 assertions passed, `index_visibility.status` `VISIBLE` |
| `feast_registry.json`, `online_readback.json`, `datahub_raw_readback.json`, `summary.txt`, `SHA256SUMS` | written by `collect` |

The read-only terminal views, also used for the screenshots:

```bash
# [READ-ONLY]
scripts/llm_agent/rag_runtime_run.sh show feast "$LABEL"     # entity, FeatureView, offline rows, identity match
scripts/llm_agent/rag_runtime_run.sh show online "$LABEL"    # 4/4 rows read from Redis DB 3, owner marker
scripts/llm_agent/rag_runtime_run.sh show summary "$LABEL"   # every stage of the run
```

If the DagRun fails, the failing task's log is in
`~/ewallet-evidence/rag/rag_$LABEL/airflow/<task>.log` after `collect`, or in
the Airflow UI. Fix the cause and trigger again with a new label; a failed run's
artifacts stay as they are.

## 4. Screenshots

The owner captures the screenshots by hand and saves the originals in
`~/Pictures/Screenshots/`. The final set comes from a Hybrid run (§5); the same
views work for a local run, which is useful for rehearsal. With the gke target,
run every `show` command in the shell of §5.1 (`RAG_TARGET=gke` exported).

| File | Where | Must be visible |
|---|---|---|
| `RAG-01_airflow_dag_success.png` | http://127.0.0.1:8095 → `rag_fraud_policy_ingestion` → the run `manual__rag-<label>` → Grid | 6 green tasks, run ID, state success |
| `RAG-02_airflow_pipeline_graph.png` | Same run → Graph | `discover_documents` → `parse_and_chunk` → `generate_embeddings` → `publish_feast_features` → `validate_online_readback` → `publish_datahub_lineage` |
| `RAG-03_feast_publication.png` | Terminal: `show feast <label>` | `fraud_policy_chunks` with its fields and `embedding Array(Float32)`; entity `rag_chunk`; 4 offline rows; `publication_identity_matches_run: true` |
| `RAG-04_redis_online_readback.png` | Terminal: `show online <label>` | 4/4 rows with `embedding_dimensions` 384; `redis.database` (3 locally, the chosen database on GKE); `owner_campaign` = the run's project |
| `RAG-05_datahub_lineage.png` | http://localhost:9002 → search the run's project (`ewallet_rag_verify` locally) → dataset `<project>.fraud_policy_chunks/fraud-policy-feast-v1` → Lineage | Corpus `fraud-policy/fraud-policy-v1-20261030` → task `publish_feast_features` (flow `rag_fraud_policy_ingestion`) → the Feast dataset |
| `RAG-06_datahub_quality_assertions.png` | Same dataset → Quality tab | 5 assertions, all with a successful latest run |
| `RAG-07_end_to_end_validation.png` | Terminal: `show summary <label>` | 6 task states, `MATCH`, recall 1.0, `PUBLISHED_AND_READ_BACK`, `VISIBLE` |

The local DataHub also holds older RAG metadata: the DataFlow
`rag_fraud_policy` and Feast datasets of the projects `ewallet_rag`,
`ewallet_rag_campaign` and `ewallet_rag_local`. Start the lineage screenshot
from the `ewallet_rag_verify` dataset or from the DataFlow
`rag_fraud_policy_ingestion`, as in the table above.

## 5. Hybrid target: GKE Feast Online Redis and GCS (cloud)

| Component | Where |
|---|---|
| Airflow and the six tasks, embedding model | Laptop (isolated Airflow, 127.0.0.1:8095) |
| DataHub | Laptop (quickstart, GMS 127.0.0.1:18080) |
| Feast registry and offline Parquet | GCS `gs://ewallet-data-ai-platform-hg26-data/features/registries/rag/<project>/` and `…/features/rag/<project>/` |
| Feast Online Store | GKE StatefulSet `feature-store/redis`, reached through `kubectl port-forward` on 127.0.0.1:16380 |
| RAG MCP server and agents | GKE; they read the production project `ewallet_rag` in DB 0 |

A Hybrid run writes only to a verification database in 2–15 and to a Feast
project `ewallet_rag_<suffix>`. The code refuses DB 0, DB 1, the project
`ewallet_rag`, a database holding another workload's keys, and GCS paths that
do not contain the project. The run does not change what the MCP server reads
(§5.6).

### 5.1 Access and wake (owner)

```bash
cd ~/Develop/ewallet-data-ai-platform && git switch main && git pull --ff-only
export PATH=$HOME/miniconda3/envs/fintech/bin:$PATH
# [READ-ONLY] credentials through the IAM-authenticated DNS endpoint (the laptop IP is not in the allowlist)
export KUBECONFIG=$HOME/.cache/ewallet-fs-cloud/kubeconfig
gcloud container clusters get-credentials ewallet-dev --zone asia-southeast1-b \
  --project ewallet-data-ai-platform-hg26 --dns-endpoint
export RAG_TARGET=gke RAG_KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
python3 ops/gcp/cloud_lab.py --kubeconfig "$KUBECONFIG" status
# [COST] owner decision: resize the existing pool (as in the Feature Store runbook §1.1)
gcloud container clusters resize ewallet-dev --node-pool cpu-pool --num-nodes 5 \
  --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26 --quiet
kubectl --context "$RAG_KUBE_CONTEXT" -n feature-store rollout status statefulset/redis --timeout=600s
```

Run every later command in this shell, so that `KUBECONFIG`, `RAG_TARGET` and
`RAG_KUBE_CONTEXT` stay exported. Feast on the laptop writes to GCS with the
Application Default Credentials. If the preflight ADC check fails, run
`gcloud auth application-default login`.

### 5.2 Choose the database and write the settings

```bash
# [CLOUD, READ-ONLY] keys and ownership marker of DB 0-15, through the runner's own port-forward
scripts/llm_agent/rag_runtime_run.sh dbs
```

Pick a database in 2–15 whose `keys` is 0, and a new project name such as
`ewallet_rag_hybrid_20261115`. Then write the settings file:

```bash
# [CLOUD read of the Secret feature-store/redis, LOCAL write] ~/.config/ewallet-rag/runtime.env, mode 600
scripts/llm_agent/rag_runtime_run.sh configure 5 ewallet_rag_hybrid_20261115 --replace
```

`configure` reads the Redis password from the Secret and writes it into the
file without printing it. It sets the following values:

- `EWALLET_RAG_ONLINE_TARGET=verification`
- `EWALLET_REDIS_CONNECTION_STRING=127.0.0.1:16380,password=…,db=<db>`
- the registry `gs://…/features/registries/rag/<project>/registry.db`
- the publication base `gs://…/features/rag/<project>`
- DataHub `http://127.0.0.1:18080`

`--replace` keeps the previous file as `runtime.env.bak-<timestamp>` (mode 600).
Without it, an existing file is left untouched. Never `cat` the settings file.

The settings file from 2026-11-02 selects DB 1 and has no DataHub URL.
Preflight fails on it until `configure` has run.

### 5.3 Start and preflight

```bash
# [LOCAL] restart the isolated Airflow so its tasks read the gke settings file
scripts/llm_agent/rag_runtime_run.sh stop
scripts/llm_agent/rag_runtime_run.sh prepare
scripts/llm_agent/rag_runtime_run.sh start
# [CLOUD, READ-ONLY] every line must be PASS
scripts/llm_agent/rag_runtime_run.sh preflight
```

The gke preflight adds these checks to the local ones:

| Check | Meaning of FAIL |
|---|---|
| kube context in `KUBECONFIG` | `KUBECONFIG` is not exported, or `get-credentials` has not run |
| GKE API reachable and `feature-store/redis` ready 1/1 | The cluster is asleep or still waking |
| port-forward 127.0.0.1:16380 (one, runner-owned) | Another process holds the port; the message lists it |
| settings select verification mode | Run `configure` |
| Redis target explicit and isolated | The DB is outside 2–15, or the project is not `ewallet_rag_<suffix>` |
| registry and publication base dedicated to the project | A `gs://` path does not contain `/<project>/` |
| database free or owned by this exact publication | See the ownership states below |
| eviction policy `noeviction` | The Redis would drop rows under memory pressure |
| ADC may create, overwrite, read and list | Run `gcloud auth application-default login`, or grant the account object access on the bucket |

The ownership check can fail in two ways:

- `NONEMPTY_UNOWNED` means the database holds another workload's keys.
- `CONFLICT` lists `differing_fields` in the target report. `target_fingerprint`
  means Redis restarted (for example after a sleep) or the port changed.

In both cases, choose another empty database and a new project with
`configure`. Never flush the database.

### 5.4 Run and verify

```bash
# [CLOUD, TARGET-WRITE] Redis DB <db>, GCS registry and Parquet under <project>, local DataHub
LABEL=hybrid-$(date -u +%Y%m%dt%H%M%S)
scripts/llm_agent/rag_runtime_run.sh trigger "$LABEL"
# [CLOUD, READ-ONLY] terminal views for RAG-03, RAG-04, RAG-07
scripts/llm_agent/rag_runtime_run.sh show feast "$LABEL"
scripts/llm_agent/rag_runtime_run.sh show online "$LABEL"
scripts/llm_agent/rag_runtime_run.sh show summary "$LABEL"
# [CLOUD, READ-ONLY] the database now holds 5 keys owned by the project; the GCS objects exist
scripts/llm_agent/rag_runtime_run.sh dbs
PROJECT=ewallet_rag_hybrid_20261115
gcloud storage ls -l "gs://ewallet-data-ai-platform-hg26-data/features/rag/$PROJECT/fraud-policy-feast-v1/" \
  "gs://ewallet-data-ai-platform-hg26-data/features/registries/rag/$PROJECT/"
```

**Port-forward lifecycle.**

- `trigger` opens one port-forward for its preflight and keeps it for the
  whole DagRun, because the tasks reach Redis through it. It reopens the
  forward if kubectl drops it, and closes it on exit, including on Ctrl-C.
- `show`, `collect` and `dbs` each manage their own forward the same way, for
  as long as they run.
- A second command started while `trigger` is running reuses that forward and
  leaves it open.
- Trigger runs only through the runner. A run started from the Airflow UI has no
  forward, so its Redis tasks fail closed, unless
  `scripts/llm_agent/rag_runtime_run.sh forward` holds one in another terminal.

A second run on the same database and project reports `OWNED`, as long as
Redis has not restarted in between.

The expected artifacts are those of §3, with Hybrid values:
`publication.json` lists the `gs://` offline source and registry, and
`online_target.redis_database` is the chosen database.

### 5.5 Hybrid screenshots

Capture RAG-01 to RAG-07 (§4) from this run, then these two:

| File | Where | Must be visible |
|---|---|---|
| `RAG-08_gke_redis_databases.png` | Terminal: `dbs` after the run | The chosen database with 5 keys and the project as owner; DB 0 and DB 1 unchanged |
| `RAG-09_gcs_feast_paths.png` | Terminal: the `gcloud storage ls -l` above | `chunks.parquet` and `registry.db` under the project's paths |

### 5.6 What the agents read

The RAG MCP server on GKE reads project `ewallet_rag` in DB 0, using the
registry snapshot from the ConfigMap `feast-registry-rag`. A Hybrid
verification run does not change either. The deployed agent path is shown by
the 2026-11-02 evidence (RAG_PIPELINE.md §11.2).

Publishing a new revision for the agents is a separate production change that
needs explicit owner approval. It means `EWALLET_RAG_ONLINE_TARGET=production`,
DB 0 and project `ewallet_rag`. This runner refuses production mode.

### 5.7 Clean up and sleep

```bash
# [LOCAL] stop Airflow and any runner port-forward; add --with-datahub to stop DataHub too
scripts/llm_agent/rag_runtime_run.sh stop
pgrep -a kubectl | grep port-forward || echo "no port-forward left"
# [COST] owner: put the cluster back to sleep
python3 ops/gcp/cloud_lab.py --kubeconfig "$KUBECONFIG" plan-sleep
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py --kubeconfig "$KUBECONFIG" sleep --execute --confirm-cluster ewallet-dev
```

The verification database with its 5 keys, and the GCS objects, stay as
evidence. Nothing is flushed or deleted; Redis keeps the keys on its PVC (AOF).
After the next wake, Redis has a new server `run_id`. The next Hybrid run
therefore needs a new empty database and a new project (§5.2).

## 6. Retrieval through the tools (optional)

For the Hybrid target, keep `scripts/llm_agent/rag_runtime_run.sh forward`
running in a second terminal, and use `EWALLET_RAG_ENV_FILE=~/.config/ewallet-rag/runtime.env`.

```bash
# [READ-ONLY] the knowledge_search and chunk_lookup classes, called directly (no MCP gateway)
EWALLET_RAG_ENV_FILE=~/.config/ewallet-rag/runtime.local.env PYTHONPATH=. \
  infra/docker/rag-ingestion/.venv/bin/python -m ops.llm_agent.rag_query \
  --query "When must an account takeover alert fail closed to manual review?" --top-k 3
```

## 7. Stop and clean up

```bash
# [LOCAL] stop the isolated Airflow and the local Redis container; DataHub keeps running
scripts/llm_agent/rag_runtime_run.sh stop
# [LOCAL] also stop DataHub (containers only; its volumes and metadata are kept)
scripts/llm_agent/rag_runtime_run.sh stop --with-datahub
```

- **Kept.** Evidence under `~/ewallet-evidence/rag/`, the local Feast registry,
  DataHub volumes, and the Airflow home with its task logs.
- **Port-forward.** With the gke target, each command closes the forward it
  opened when it exits, also on Ctrl-C and SIGTERM. `stop` also closes a
  forward left by a command that was killed with SIGKILL. It closes only a
  process whose command line is the runner's forward. To check:
  `pgrep -a kubectl | grep port-forward`.
- **Cluster.** If the owner woke the cluster for §5, put it back to sleep (§5.7).
- **Retained evidence.** Copy the run directory into
  `docs/evidence/edai2/rag/<label>/` without `source/`, then check it with
  `sha256sum -c SHA256SUMS`.
