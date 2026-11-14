# RAG Hybrid screenshots

The owner captured these screenshots by hand on 2026-11-14 from the RAG Hybrid
campaign, run label `hybrid-20261114t151101`. They were copied unchanged from
`~/Pictures/Screenshots/`, with the same file names. `SHA256SUMS` lists the
digest of each file, and each digest was checked against its original. The
screenshots are explained in [RAG_PIPELINE.md](../../../../edai2/rag/RAG_PIPELINE.md).

Common identifiers:

| Item | Value |
|---|---|
| Source commit | `9153a06af5e89edbb3a60b5c2fd95d73eb3d6c0a` |
| DagRun | `manual__rag-hybrid-20261114t151101` of `rag_fraud_policy_ingestion` |
| Feast project | `ewallet_rag_hybrid_20261114t144825` |
| Online Store | GKE `feature-store/redis`, DB 3 |
| Raw evidence | [`../hybrid-20261114t151101/`](../hybrid-20261114t151101/README.md) |

Screenshots and what each one shows:

| File | Shows | Implementation | Supporting raw evidence |
|---|---|---|---|
| `RAG-01_airflow_dag_success.png` | Airflow 3 UI, Graph view and task list of the DagRun: the six tasks in order, all `success`, run state Success, duration 00:00:39.833 | DAG [`rag_fraud_policy_ingestion.py`](https://github.com/HuyyGiaa/ewallet-data-ai-platform/blob/9153a06af5e89edbb3a60b5c2fd95d73eb3d6c0a/data_platform/orchestration/airflow/dags/rag_fraud_policy_ingestion.py#L36-L67) | [`airflow/task_states.json`](../hybrid-20261114t151101/airflow/task_states.json), [`airflow/dag_runs.json`](../hybrid-20261114t151101/airflow/dag_runs.json), task logs |
| `RAG-03_feast_publication.png` | A `jq` selection of `feast_registry.json`: project, GCS registry and Parquet paths, entity `rag_chunk`, FeatureView `fraud_policy_chunks` (online, `Array(Float32)` embedding), 4 offline rows, 384 dimensions, `publication_identity_matches_run: true`, materialization window | `feast_view()` in [`rag_evidence.py`](https://github.com/HuyyGiaa/ewallet-data-ai-platform/blob/9153a06af5e89edbb3a60b5c2fd95d73eb3d6c0a/ops/llm_agent/rag_evidence.py#L49-L71) | [`feast_registry.json`](../hybrid-20261114t151101/feast_registry.json), [`publication.json`](../hybrid-20261114t151101/publication.json) |
| `RAG-04a_redis_online_readback.png` | `rag_runtime_run.sh show online`: the target (verification, DB 3, `CLAIMED`), `requested: 4`, and the first rows read back from Redis with 384 dimensions and L2 norm 1.0 | `online_view()` in [`rag_evidence.py`](https://github.com/HuyyGiaa/ewallet-data-ai-platform/blob/9153a06af5e89edbb3a60b5c2fd95d73eb3d6c0a/ops/llm_agent/rag_evidence.py#L74-L101) | [`online_readback.json`](../hybrid-20261114t151101/online_readback.json) |
| `RAG-04b_redis_online_readback.png` | The rest of the same output: the fourth row, `returned: 4`, Redis DB 3, `dbsize` 5, owner campaign `ewallet_rag_hybrid_20261114t144825`, owner store `rag-verification` | same | same |
| `RAG-05_datahub_lineage.png` | DataHub UI, Lineage tab of the Feast dataset: the corpus dataset `fraud-policy corpus fraud-policy-v1-20261030` and the task `publish_feast_features` (flow `rag_fraud_policy_ingestion`) upstream of `fraud_policy_chunks fraud-policy-feast-v1` | `lineage_records()` in [`governance.py`](https://github.com/HuyyGiaa/ewallet-data-ai-platform/blob/9153a06af5e89edbb3a60b5c2fd95d73eb3d6c0a/ai_platform/llm_agent/rag/governance.py#L107-L162) | [`datahub.json`](../hybrid-20261114t151101/datahub.json), [`datahub_raw_readback.json`](../hybrid-20261114t151101/datahub_raw_readback.json) |
| `RAG-06_datahub_quality_assertions.png` | DataHub UI, Quality → Assertions of the same dataset: `Custom (5)`, all five with a green latest result. The category label is the custom assertion type `E-Wallet RAG Data Contract` | `quality_checks()`, `assertion_records()` in [`governance.py`](https://github.com/HuyyGiaa/ewallet-data-ai-platform/blob/9153a06af5e89edbb3a60b5c2fd95d73eb3d6c0a/ai_platform/llm_agent/rag/governance.py#L72-L104) | [`datahub.json`](../hybrid-20261114t151101/datahub.json) (`quality_checks`) |
| `RAG-07a_end_to_end_execution.png` | `rag_runtime_run.sh show summary`, first part: run and DagRun IDs, six task states, documents, chunks, embeddings, publication with GCS paths and the Redis target | `summary_view()` in [`rag_evidence.py`](https://github.com/HuyyGiaa/ewallet-data-ai-platform/blob/9153a06af5e89edbb3a60b5c2fd95d73eb3d6c0a/ops/llm_agent/rag_evidence.py#L104-L128) | [`summary.txt`](../hybrid-20261114t151101/summary.txt) |
| `RAG-07b_end_to_end_validation.png` | The same output, second part: readback `MATCH`, 4 online rows, recall@k 1.0, MRR 1.0, 0 citation errors, DataHub `PUBLISHED_AND_READ_BACK`, 27 aspects, five assertions `true`, index `VISIBLE` | same | [`summary.txt`](../hybrid-20261114t151101/summary.txt), [`validation.json`](../hybrid-20261114t151101/validation.json), [`datahub.json`](../hybrid-20261114t151101/datahub.json) |
| `RAG-08_gke_redis_databases.png` | `rag_runtime_run.sh dbs` after the run: keys and ownership marker of DB 0–15 on the GKE Redis; DB 3 holds 5 keys owned by the campaign | `dbs()` in [`rag_runtime_run.sh`](https://github.com/HuyyGiaa/ewallet-data-ai-platform/blob/9153a06af5e89edbb3a60b5c2fd95d73eb3d6c0a/scripts/llm_agent/rag_runtime_run.sh#L334-L361) | — (terminal only; see the campaign README) |
| `RAG-09_gcs_feast_paths.png` | `gcloud storage ls -l` of the two GCS objects: `chunks.parquet` 19,943 bytes at 15:11:25Z and `registry.db` 2,275 bytes at 15:11:31Z | Written by `write_offline_source()` and `store.apply()` in `publish()`; `gcloud` only lists them | [`publication.json`](../hybrid-20261114t151101/publication.json) (`feast`) |

## Notes

- **No RAG-02.** RAG-01 already shows the Graph view, the task dependencies,
  all six task states and the DagRun ID, so a separate Graph capture was not
  taken. The set is 10 files covering 8 evidence groups (RAG-04 and RAG-07
  each span two images).
- **RAG-03 command line.** In RAG-03 the echoed `jq` command is partly
  overwritten by the terminal's line editing. The JSON below it is the
  selection from `feast_registry.json` and matches that file.
- **RAG-08 is the state after the run.** The owner's before-and-after key
  counts are recorded in the [campaign README](../hybrid-20261114t151101/README.md#redis-database-isolation).
  There is no screenshot of the state before the run.
- **Terminal warnings.** RAG-04a shows a `FutureWarning` from
  `google.api_core` about Python 3.10. It does not affect the result.
