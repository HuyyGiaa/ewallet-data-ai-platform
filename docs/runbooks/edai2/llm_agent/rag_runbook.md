# RAG runbook

This runbook runs the RAG ingestion DAG `rag_fraud_policy_ingestion` from a
clean checkout of `main`, verifies the result, and captures the runtime
screenshots. The design is explained in
[RAG_PIPELINE.md](../../../edai2/rag/RAG_PIPELINE.md). Every command runs from
the repository root.

## Safety classes

| Label | Effect |
|---|---|
| `READ-ONLY` | Reads files, Airflow, Feast, Redis or DataHub; changes nothing |
| `LOCAL` | Starts or stops local processes and containers, or writes local files |
| `TARGET-WRITE` | Writes Feast rows and the ownership marker to the selected Redis database, and metadata to DataHub |
| `CLOUD` | Needs or uses the GKE cluster or GCS; only for the `gke` target |

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
  tests/llm_agent/test_rag_governance_visibility.py tests/llm_agent/test_phase_abc_p0_fixes.py
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
`~/Pictures/Screenshots/`.

| File | Where | Must be visible |
|---|---|---|
| `RAG-01_airflow_dag_success.png` | http://127.0.0.1:8095 → `rag_fraud_policy_ingestion` → the run `manual__rag-<label>` → Grid | 6 green tasks, run ID, state success |
| `RAG-02_airflow_pipeline_graph.png` | Same run → Graph | `discover_documents` → `parse_and_chunk` → `generate_embeddings` → `publish_feast_features` → `validate_online_readback` → `publish_datahub_lineage` |
| `RAG-03_feast_publication.png` | Terminal: `show feast <label>` | `fraud_policy_chunks` with its fields and `embedding Array(Float32)`; entity `rag_chunk`; 4 offline rows; `publication_identity_matches_run: true` |
| `RAG-04_redis_online_readback.png` | Terminal: `show online <label>` | 4/4 rows with `embedding_dimensions` 384; `redis.database` 3; `owner_campaign` `ewallet_rag_verify` |
| `RAG-05_datahub_lineage.png` | http://localhost:9002 → search `ewallet_rag_verify` → dataset `ewallet_rag_verify.fraud_policy_chunks/fraud-policy-feast-v1` → Lineage | Corpus `fraud-policy/fraud-policy-v1-20261030` → task `publish_feast_features` (flow `rag_fraud_policy_ingestion`) → the Feast dataset |
| `RAG-06_datahub_quality_assertions.png` | Same dataset → Quality tab | 5 assertions, all with a successful latest run |
| `RAG-07_end_to_end_validation.png` | Terminal: `show summary <label>` | 6 task states, `MATCH`, recall 1.0, `PUBLISHED_AND_READ_BACK`, `VISIBLE` |

The local DataHub also holds older RAG metadata: the DataFlow
`rag_fraud_policy` and Feast datasets of the projects `ewallet_rag`,
`ewallet_rag_campaign` and `ewallet_rag_local`. Start the lineage screenshot
from the `ewallet_rag_verify` dataset or from the DataFlow
`rag_fraud_policy_ingestion`, as in the table above.

## 5. GKE target (cloud; only after the owner wakes the cluster)

This variant writes to the GKE Feast Online Redis and to GCS. Waking the
cluster and choosing the database are owner decisions; the runner does neither.
See the [cloud lab runbook](../CLOUD_LAB_RUNBOOK.md) for waking and sleeping.

```bash
export RAG_TARGET=gke RAG_KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
# [CLOUD, READ-ONLY] find an empty database in 2-15 (DB 0 production, DB 1 canonical-verify, others may be campaigns)
kubectl --context "$RAG_KUBE_CONTEXT" -n feature-store exec statefulset/redis -c redis -- \
  sh -c 'for db in $(seq 2 15); do printf "db %s size " $db; REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --no-auth-warning -n $db DBSIZE; done'
```

Then edit `~/.config/ewallet-rag/runtime.env` (mode 600) without printing it,
and set these values:

| Setting | Value |
|---|---|
| `EWALLET_RAG_ONLINE_TARGET` | `verification` |
| `EWALLET_RAG_FEAST_PROJECT` | `ewallet_rag_verify_<yyyymmdd>` |
| `EWALLET_RAG_FEAST_REGISTRY` | `gs://ewallet-data-ai-platform-hg26-data/features/registries/rag/runs/<label>/registry.db` |
| `EWALLET_RAG_PUBLICATION_BASE` | `gs://ewallet-data-ai-platform-hg26-data/features/rag/runs/<label>` |
| `EWALLET_REDIS_CONNECTION_STRING` | `127.0.0.1:16380,password=<from the Secret>,db=<chosen empty db>` |
| `DATAHUB_GMS_URL` | `http://127.0.0.1:18080` |

The current file still selects DB 1 from the 2026-11-02 campaign. The new code
refuses DB 1, so change it to the chosen database.

```bash
# [LOCAL] restart the isolated Airflow so its tasks read the gke settings file
scripts/llm_agent/rag_runtime_run.sh stop
RAG_TARGET=gke scripts/llm_agent/rag_runtime_run.sh start
# [CLOUD, READ-ONLY] preflight opens the port-forward for its checks
RAG_TARGET=gke scripts/llm_agent/rag_runtime_run.sh preflight
# [CLOUD, TARGET-WRITE] Redis DB <chosen>, GCS registry and Parquet, local DataHub
RAG_TARGET=gke scripts/llm_agent/rag_runtime_run.sh trigger gke-$(date -u +%Y%m%dt%H%M%S)
```

The Jobs need the GCS permissions of the account that runs Airflow
(`gcloud auth application-default login`), with write access to the two run
prefixes. The rows stay in the chosen database. The production MCP tool keeps
reading project `ewallet_rag` in DB 0; promoting a new revision there
(`EWALLET_RAG_ONLINE_TARGET=production`) is a separate, explicit owner change.

## 6. Retrieval through the tools (optional)

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
- **Port-forward.** With the gke target, the runner closes its own forward
  when each command exits. To find a stray forward:
  `pgrep -a kubectl | grep port-forward`, then stop it by PID.
- **Cluster.** If the owner woke the cluster for §5, put it back to sleep with
  `python3 ops/gcp/cloud_lab.py plan-sleep`, then
  `CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py sleep --execute --confirm-cluster ewallet-dev`.
- **Retained evidence.** Copy the run directory into
  `docs/evidence/edai2/rag/<label>/` without `source/`, then check it with
  `sha256sum -c SHA256SUMS`.
