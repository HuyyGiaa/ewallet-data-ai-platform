# RAG Hybrid campaign `hybrid-20261114t151101`

These are the raw artifacts of the owner-run RAG Hybrid campaign of
2026-11-14. The pipeline, the code and the interpretation are described in
[RAG_PIPELINE.md](../../../../edai2/rag/RAG_PIPELINE.md#111-hybrid-campaign-2026-11-14).
The owner's screenshots are in [`../screenshots/`](../screenshots/README.md).

## Identity

| Item | Value | Recorded in |
|---|---|---|
| Source commit | `9153a06af5e89edbb3a60b5c2fd95d73eb3d6c0a` | `airflow/git_sha.txt`, `release_sha` in every stage artifact |
| DAG | `rag_fraud_policy_ingestion` | `airflow/dag_runs.json` |
| DagRun | `manual__rag-hybrid-20261114t151101`, 15:11:14.99 → 15:11:54.82 UTC | `airflow/run_id.txt`, `airflow/dag_runs.json` |
| Run label | `hybrid-20261114t151101` | `run_id` in every stage artifact |
| Feast project | `ewallet_rag_hybrid_20261114t144825` | `publication.json` |
| Online Store | GKE `ewallet-dev`, StatefulSet `feature-store/redis`, DB 3, mode `verification`, claim `CLAIMED` | `publication.json`, `online_readback.json` |
| Registry | `gs://ewallet-data-ai-platform-hg26-data/features/registries/rag/ewallet_rag_hybrid_20261114t144825/registry.db` | `publication.json` |
| Offline Parquet | `gs://ewallet-data-ai-platform-hg26-data/features/rag/ewallet_rag_hybrid_20261114t144825/fraud-policy-feast-v1/chunks.parquet` | `publication.json` |
| Task runtime | Python 3.10.22, Feast 0.66.0, onnxruntime 1.23.2, acryl-datahub 1.7.0.5; `uv.lock` SHA-256 `20c00145…` | `documents.json` (`runtime`) |

## Topology

| Component | Ran on |
|---|---|
| Airflow 3.3.1 (isolated instance, UI 127.0.0.1:8095) and the six tasks | Owner's Ubuntu laptop |
| Document processing and ONNX embedding inference | Owner's laptop |
| DataHub quickstart (GMS 127.0.0.1:18080) | Owner's laptop, Docker |
| Feast registry and offline Parquet | Google Cloud Storage |
| Feast Online Store | Redis on GKE, DB 3, reached through the runner's `kubectl port-forward` on 127.0.0.1:16380 |

The run was triggered with `scripts/llm_agent/rag_runtime_run.sh trigger
hybrid-20261114t151101`. That command runs the preflight, starts the DagRun,
waits for it to finish, and then runs `collect`.

## Inventory

| File | Written by | Content |
|---|---|---|
| `airflow/run_id.txt`, `airflow/git_sha.txt` | runner `trigger`, `collect` | DagRun ID, checkout commit at collection time |
| `airflow/dag_runs.json`, `airflow/task_states.json` | runner `collect` (Airflow CLI) | Run state `success`; six task instances `success` |
| `airflow/<task>.log` (6 files) | runner `collect` | Airflow task logs (attempt 1 of each task) |
| `documents.json` | task `discover_documents` | 2 documents, SHA-256 of each, 0 mismatches, manifest digest, runtime identity |
| `chunks.json` | task `parse_and_chunk` | 4 chunks (80 words, overlap 16), `deterministic: true` |
| `embeddings.json` | task `generate_embeddings` | 4 vectors of 384 dimensions, 0 invalid, norms 0.99999996–1.00000004, model file digest `6fd5d72f…` |
| `publication.json` | task `publish_feast_features` | 4 rows, identity `b85ae796…`, offline `WRITTEN`, materialization window, claim `CLAIMED` |
| `validation.json` | task `validate_online_readback` | readback `MATCH`, 4 online rows, ownership `MATCH`, both evaluation queries with their ranked citations |
| `datahub.json` | task `publish_datahub_lineage` | `PUBLISHED_AND_READ_BACK`, 27 aspects read back as `MATCH`, five quality checks, index visibility `VISIBLE` (all five checks found on the first attempt) |
| `feast_registry.json` | runner `collect` (`rag_evidence feast`) | Registry objects read from GCS, offline rows read from the Parquet, recomputed publication identity |
| `online_readback.json` | runner `collect` (`rag_evidence online`) | 4 of 4 rows read from Redis DB 3 through Feast, `dbsize` 5, owner marker |
| `datahub_raw_readback.json` | runner `collect` | GMS readback of the Feast dataset's `upstreamLineage` and the DataFlow's `dataFlowInfo` |
| `summary.txt` | runner `collect` (`rag_evidence summary`) | The terminal summary shown in RAG-07a/b |
| `SHA256SUMS` | runner `collect` | Digests of the 20 files above |

The three files written by `collect` through `rag_evidence` (`feast_registry.json`,
`online_readback.json`, `summary.txt`) are read separately from the task
artifacts. They are not copies of what the tasks reported.

## Results

| Check | Result |
|---|---|
| Airflow | 6/6 tasks `success`; DagRun 39.8 s |
| Documents | 2, 0 checksum mismatches |
| Chunks | 4, deterministic |
| Embeddings | 4 × 384, L2-normalized, 0 invalid; model `sentence-transformers/all-MiniLM-L6-v2` at `1110a243fdf4706b3f48f1d95db1a4f5529b4d41` |
| Feast | Entity `rag_chunk` (join key `chunk_id`), FeatureView `fraud_policy_chunks`, 4 offline rows, identity `b85ae796221aa0b9e2adfd7ff19488f612dff18c8d60c3c44f0134b7266bf549`, `publication_identity_matches_run: true` |
| Redis | DB 3: requested 4, returned 4, each 384-d with norm 1.0; `dbsize` 5; owner `ewallet_rag_hybrid_20261114t144825` / `rag-verification` |
| Retrieval | 2 evaluation queries, top-k 2: recall@k 1.0, MRR 1.0, empty retrieval 0, citation errors 0 |
| DataHub | 27 aspects read back; 5/5 custom assertions passed; search and lineage index `VISIBLE` |

## Redis database isolation

The owner ran `rag_runtime_run.sh dbs` before and after the campaign. The key
counts below are the owner's observations. Only the state after the run was
kept, as RAG-08; no file from the run before it was retained.

| DB | Before | After |
|---:|---:|---:|
| 0 | 173,382 | 173,382 |
| 1 | 4 | 4 |
| 2 | 204,934 | 204,934 |
| 3 | 0 | 5 |
| 8 | 27 | 27 |
| 9 | 27 | 27 |

Only DB 3 changed. The 5 keys are consistent with the 4 published chunk
entity keys plus the ownership marker. Equal key counts show that no keys were
added to or removed from the other databases. They do not prove that the
values in those databases are byte-for-byte unchanged; no content comparison
was made.

## GCS objects

The owner listed the two objects with `gcloud storage ls -l` (RAG-09). This
only reads the bucket metadata; the pipeline wrote the objects.

| Object | Size | Created |
|---|---:|---|
| `…/ewallet_rag_hybrid_20261114t144825/fraud-policy-feast-v1/chunks.parquet` | 19,943 bytes | 2026-11-14T15:11:25Z |
| `…/registries/rag/ewallet_rag_hybrid_20261114t144825/registry.db` | 2,275 bytes | 2026-11-14T15:11:31Z |

## Provenance and checksums

- **Source.** Copied from the owner's run directory
  `~/ewallet-evidence/rag/rag_hybrid-20261114t151101/`, with timestamps
  preserved and the bytes unchanged.
- **Manifest.** The runner-generated `SHA256SUMS` covers exactly the 20
  files imported here, with the same relative paths, so it is the original
  manifest, unchanged. `sha256sum -c SHA256SUMS` in this folder passes for all
  20 files.
- **Omitted.** `source/` is omitted: `manifest.json`,
  `policy-account-takeover.md` and `policy-payment-anomaly.md`. It is not in
  the runner manifest, and all three files were checked to be byte-identical
  to [`data/llm_agent/rag/fraud-policy-v1/`](../../../../../data/llm_agent/rag/fraud-policy-v1/).
- **Secrets.** Before import, every file was searched for the Redis password
  from the run's settings file, and for tokens, keys, credentials,
  authorization headers, account e-mail addresses and IP addresses. Nothing
  sensitive was found:
  - The task logs list the *names* of the settings they loaded, such as
    `EWALLET_REDIS_CONNECTION_STRING`, but never their values.
  - The only addresses are the loopback DataHub URL `http://127.0.0.1:18080`.
  - The settings file `~/.config/ewallet-rag/runtime.env` and its backups were
    not copied.

## Limitations

- The corpus has 2 documents and 4 chunks. The retrieval metrics come from 2
  predefined queries and do not measure retrieval quality at scale.
- This campaign published to the verification project in DB 3. The production
  RAG MCP server and the agents read project `ewallet_rag` in DB 0, so this run
  does not show agents consuming the new publication.
- The Redis isolation result rests on key counts (see above), not on a content
  comparison.
