# RAG runbook: reproduce and capture evidence from a clean checkout

Rubric edai-2 R9/R10 (RAG Data Pipeline + governance), R16 (chunk by id), R33 (CI/CD).
Design and status: `docs/llm_agent/phase_c_rag.md`. Every command runs from the repository
root of the checked-out commit. No old worktree, manual pod edit or untracked file is needed.

## Prerequisites

| Item | Version / identity |
|---|---|
| uv | 0.13.5 |
| RAG task runtime | Python 3.10 (`infra/docker/rag-ingestion`: `pyproject.toml` + `uv.lock`) |
| Airflow | 3.3.1 (existing local install, conda env `airflow`, `AIRFLOW_HOME=~/airflow`; its DAGs folder must be this checkout) |
| Feast Online Redis | existing `feature-store/svc/redis` on GKE `ewallet-dev` |
| DataHub | existing local quickstart v1.7.0.1 (`data_platform/metadata/datahub/runtime/start_datahub.sh`) |
| Embedding model | `sentence-transformers/all-MiniLM-L6-v2@1110a243fdf4706b3f48f1d95db1a4f5529b4d41` (ONNX, downloaded on first use into `HF_HOME`) |

## 1. Install and test (no cloud)

```bash
uv lock --check --project infra/docker/rag-ingestion
uv sync --locked --project infra/docker/rag-ingestion
uv run --locked --project infra/docker/rag-ingestion python -m pytest -q \
  tests/llm_agent/test_rag_airflow_ingestion.py tests/llm_agent/test_rag_online_retriever.py \
  tests/llm_agent/test_rag_chunk_lookup.py tests/llm_agent/test_phase_c.py
bash scripts/validation/validate_rag_dag_import.sh ~/miniconda3/envs/airflow/bin/python /tmp/rag-dag-parse.json
uv sync --locked --no-dev --project infra/docker/rag-ingestion      # the interpreter the DAG tasks run
```

Pass: lock consistent, all tests pass (1 opt-in model test skipped), parse report has no import errors.

## 2. Optional local orchestration check (no cloud)

```bash
scripts/validation/run_rag_dag_isolated.sh ~/miniconda3/envs/airflow/bin/airflow local-check-01
# with the local DataHub running (step 3a):
DATAHUB_GMS_URL=http://127.0.0.1:18080 \
  scripts/validation/run_rag_dag_isolated.sh ~/miniconda3/envs/airflow/bin/airflow local-check-02 publish
```

This runs the six-task `rag_fraud_policy_ingestion` DAG with real Airflow
`dags test` in a throwaway home and the sqlite Feast profile. Pass: 6/6
tasks succeed and `docs/evidence/llm_agent/rag_<label>/validation.json` shows
`readback_status=MATCH` and `recall_at_k=1.0`. In publish mode, `datahub.json` also shows
`PUBLISHED_AND_READ_BACK`. This is local evidence, not canonical runtime evidence.

## 3. Canonical Airflow run (existing Airflow + GKE Redis + DataHub)

a. DataHub. The existing quickstart keeps its volumes. GMS moves to 18080 because Airflow
   holds 8080:

```bash
DATAHUB_PYTHON="$HOME/miniconda3/envs/datahub/bin/python" DATAHUB_PULL_IMAGES=false DATAHUB_MAPPED_GMS_PORT=18080 \
  ./data_platform/metadata/datahub/runtime/start_datahub.sh start
curl -fsS http://127.0.0.1:18080/health
```

b. Runtime settings. Write them once and keep them private. Values are never printed:

```bash
install -d -m 700 ~/.config/ewallet-rag
install -m 600 infra/docker/rag-ingestion/runtime.env.example ~/.config/ewallet-rag/runtime.env
REDIS_PASSWORD="$(kubectl --context gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev \
  -n feature-store get secret redis -o jsonpath='{.data.password}' | base64 -d)"
sed -i \
  -e "s#^EWALLET_RAG_FEAST_REGISTRY=.*#EWALLET_RAG_FEAST_REGISTRY=gs://ewallet-data-ai-platform-hg26-data/features/registries/rag/fraud-policy-feast-v1/registry.db#" \
  -e "s#^EWALLET_RAG_PUBLICATION_BASE=.*#EWALLET_RAG_PUBLICATION_BASE=gs://ewallet-data-ai-platform-hg26-data/features/rag#" \
  -e "s#^DATAHUB_GMS_URL=.*#DATAHUB_GMS_URL=http://127.0.0.1:18080#" \
  -e "s#^HF_HOME=.*#HF_HOME=$HOME/.cache/ewallet-rag-hf#" -e "/^GOOGLE_APPLICATION_CREDENTIALS=/d" \
  ~/.config/ewallet-rag/runtime.env
# The password goes through the environment, not argv, and may contain any character.
REDIS_PASSWORD="${REDIS_PASSWORD}" python3 - ~/.config/ewallet-rag/runtime.env <<'PY'
import os, re, sys
path = sys.argv[1]
text = open(path).read()
line = "EWALLET_REDIS_CONNECTION_STRING=127.0.0.1:16380,password=" + os.environ["REDIS_PASSWORD"]
open(path, "w").write(re.sub(r"(?m)^EWALLET_REDIS_CONNECTION_STRING=.*$", lambda _: line, text))
PY
unset REDIS_PASSWORD
```

c'. Optional rehearsal on the real backends before the live Airflow is available. It uses real
   Airflow `dags test` in a throwaway home, with the GKE Redis, the RAG registry, GCS and DataHub:

```bash
kubectl --context gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev \
  -n feature-store port-forward svc/redis 16380:6379 &
REDIS_FORWARD_PID=$!
RAG_ISOLATED_ONLINE_STORE=redis \
EWALLET_RAG_ENV_FILE="$HOME/.config/ewallet-rag/runtime.env" \
  scripts/validation/run_rag_dag_isolated.sh \
  "$HOME/miniconda3/envs/airflow/bin/airflow" gke-rehearsal-01 plan_only
kill "${REDIS_FORWARD_PID}"
```

`runtime.env` must use a new immutable run-scoped GCS publication base and
registry path plus an isolated Redis database/keyspace. `plan_only` is the
safe default: it constructs the complete DataHub proposal and assertions but
does not publish them. Use `publish` only when the existing DataHub GMS has
passed its health check and `DATAHUB_GMS_URL` is set; a plan-only result must
never be reported as DataHub runtime publication evidence.

c. Preflight, then trigger. The script keeps a `kubectl port-forward` to `feature-store/svc/redis`
   open on 127.0.0.1:16380 for the whole run, then collects the evidence:

```bash
export RAG_KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
scripts/llm_agent/rag_runtime_run.sh preflight            # every line must be PASS
scripts/llm_agent/rag_runtime_run.sh trigger gke-$(date -u +%Y%m%dt%H%M%S)
```

Pass criteria in `docs/evidence/llm_agent/rag_gke-<ts>/`:
- `airflow/task_states.json`: all 6 tasks `success`.
- `documents.json`: 2 documents, checksums verified.
- `chunks.json`: 4 deterministic chunks.
- `embeddings.json`: 384-d, model revision as above.
- `publication.json`: `feast.profile=canonical`, `online_store=redis`, 4 rows.
- `validation.json`: `readback_status=MATCH`, `recall_at_k=1.0`, `citation_errors=0`.
- `datahub.json`: `PUBLISHED_AND_READ_BACK` with 25 aspects and 5 passing assertions.
- `datahub_raw_readback.json`: the lineage edge corpus → `ewallet_rag.fraud_policy_chunks/...`.

Screenshots to capture by hand:
- Airflow Graph and Grid of the run: http://127.0.0.1:8080/dags/rag_fraud_policy_ingestion
- DataHub lineage of the Feast dataset and its Quality tab: http://127.0.0.1:9002

d. Retrieval against the real Redis. This is a direct tool call, not MCP evidence:

```bash
kubectl --context "$RAG_KUBE_CONTEXT" -n feature-store port-forward svc/redis 16380:6379 &
infra/docker/rag-ingestion/.venv/bin/python -m ops.llm_agent.rag_query \
  --query "When must an account takeover alert fail closed to manual review?" --top-k 3
infra/docker/rag-ingestion/.venv/bin/python -m ops.llm_agent.rag_query --chunk-id policy-payment-anomaly:0000
kill %1
```

## 4. MCP (after the coordinated feature-rag-mcp change)

feature-rag-mcp needs these env values:
- `EWALLET_RAG_RETRIEVER=feast-online-exact`
- `EWALLET_RAG_EMBEDDER=local-onnx`
- `EWALLET_RAG_FEAST_REGISTRY=<same registry as 3b>`
- `EWALLET_RAG_MODEL_CACHE=<writable dir>`

The image also needs onnxruntime/tokenizers/huggingface-hub in `infra/docker/llm-agent-tooling`.
Clients need the `chunk_lookup` grant and gateway allowlist entry in the Helm values. All of
these touch G1-owned files and are not part of this runbook yet.

## Cleanup and rollback

- Evidence of a failed attempt stays. Re-run with a new label; artifacts are create-only.
- Port-forwards end with the script. Stop DataHub without losing data with
  `./data_platform/metadata/datahub/runtime/start_datahub.sh stop`.
- Feast: the RAG registry and the parquet under `features/rag/<revision>` belong to RAG only.
  To roll back, re-materialize the previous revision, or delete the RAG registry object. The
  Redis keys of project `ewallet_rag` do not overlap with the historical features.
- Never edit the MLOps online registry (`features/registries/canonical-100gb-v1/online/...`).
