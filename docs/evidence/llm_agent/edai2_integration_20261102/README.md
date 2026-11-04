# EDAI-2 accelerated integration evidence — 2026-11-02

## A. Executive summary

This campaign completed the three bounded priorities without changing the
stable G3 Coordinator/Specialist business implementation, G1/G2 serving,
KServe model, feature contract, cluster-wide IAM/RBAC, or existing Secret
values.

| Workstream | Verdict | Runtime evidence |
|---|---|---|
| RAG DB1 | `MCP_CANARY_READ_AFTER_WRITE_PASS` | Official MCP SDK returned four exact chunks from the immutable Airflow publication in Redis DB1. |
| Production DB0 | `NOT_PERFORMED` | Production `feature-rag-mcp` stayed on DB0 and its image/ready state was read back unchanged. |
| DataHub | `PUBLISHED_AND_READ_BACK` | GMS accepted and returned all 25 records, including lineage and five assertion results. |
| Observability | `RUNTIME_VERIFIED_PARTIAL_INSTRUMENTATION` | Collector, Prometheus, Tempo, Loki and Grafana are healthy; a real G3 request was persisted as a 79-span trace across Coordinator and Feature-RAG Specialist. |
| Stable G3 | `PASS` | Final synthetic request returned real Feast features, KServe model result and RAG citations in 0.483 s; all core pods remained ready with zero restarts. |

The remaining observability gap is instrumentation breadth, not backend
availability: A2A Gateway, MCP Gateway, Feast and KServe do not emit their own
named services in this trace. Their calls are visible only as client/tool spans
from the instrumented Specialist. Loki is healthy but application OTLP logging
is disabled, so application-log ingestion was not claimed.

## B. Git provenance

- Worktree: `/home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/codex-llm-g2`
- Branch: `codex/llm-g2-llmd-routing`
- Initial HEAD: `34494d44299f08a7751af66b86f6af55e0b9d5fe`
- Source implementation HEAD before this evidence commit:
  `9d6ab73ef9015b61ac5588b1eb4760b5dfe17362`
- Canonical/main was not modified or advanced.
- Existing unrelated untracked evidence directories were preserved and were
  not included in this campaign.

Campaign source commits, in order:

1. `737f92b` — bounded design and execution plan.
2. `d1da0fa` — isolated DB1 MCP canary renderer/caller and regression tests.
3. `908d733` — Redis connection syntax supporting explicit DB isolation.
4. `93163bf` — production MCP authentication subjects preserved in canary.
5. `81a2b21` — production service-token header used by official MCP client.
6. `38b8030` — namespace-scoped observability stack mode.
7. `4e07a6b` — observability deployment does not roll existing MCP services.
8. `5df66f2` — numeric non-root Prometheus UID/GID.
9. `9d6ab73` — exact Coordinator/Specialist OTLP identities allowed.

## C. RAG production-equivalent read-after-write

- Airflow DAG run:
  `rag_fraud_policy_ingestion/manual__2026-11-02T04:47:47.888480+00:00`
  (run label `g3-functional-rag-20261102t045700z`), 6/6 tasks successful.
- Immutable registry:
  `gs://ewallet-data-ai-platform-hg26-data/features/registries/rag/runs/g3-functional-rag-20261102t045700z/registry.db`
- Registry SHA-256:
  `77aaae132c82d4c7ce05ad103b55194fd389a46bca2113ceb7359f823ddc7cd9`
- Publication identity:
  `b85ae796221aa0b9e2adfd7ff19488f612dff18c8d60c3c44f0134b7266bf549`
- Redis DB1 readback: four matching keys; two document IDs and four
  deterministic chunk IDs.
- Canary reused the production image digest
  `sha256:b80c07c76e4d240328804218c46ff66ecc7e4ba45694072f94774c57548fa058`,
  exact registry and normal MCP authentication.
- Actual `knowledge_search` returned four verified citations containing the
  expected document ID, chunk ID, checksum, corpus revision, embedding revision
  and feature revision.
- Missing and wrong credentials both returned HTTP 401.
- All canary-owned Deployment, ConfigMap, Secret and NetworkPolicies were
  removed. No Service or cluster-scoped object was created.
- Production DB0 routing was not changed.

Primary files: `rag/read-after-write-verdict.json`,
`rag/mcp-db1-response.json`, `rag/canary-redis-db1-readback.json`,
`rag/negative_auth_status.txt`, `rag/production-unchanged.json`, and
`rag/cleanup.txt`.

## D. DataHub governance

The existing persistent DataHub 1.7.0.1 Quickstart volumes were reused. Only
MySQL, OpenSearch, Kafka and GMS were started, serially and with memory
checkpoints. Frontend and Actions were not started. No cloud node was added.

- GMS was available and accepted the real RAG plan.
- 25/25 metadata records were published and matched on API readback.
- Published coverage: source corpus, Feast dataset/schema, upstream lineage,
  Airflow data flow, five data jobs and their IO, five assertion definitions,
  and five assertion run events.
- Assertion run readback was retried four times to allow Kafka/search indexing;
  the final values matched.
- Five real quality checks passed: source checksums, deterministic unique
  chunks, finite 384-dimensional embeddings, online/offline row equality, and
  cited retrieval (`recall@k=1.0`, zero citation errors).
- The raw Feast entity API readback proves dataset properties, schema and
  source-to-Feast lineage with the exact Airflow run and publication identity.
- Containers were stopped after publication; persistent volumes were kept.

Primary files: `datahub/publication-readback.json`,
`datahub/feast-entity-api-readback.json`, `datahub/memory-checkpoints.txt`, and
`datahub/container-stop-readback.txt`.

## E. Observability runtime

Helm `llm-agent` progressed from revision 21 to 24:

- revision 22: namespace-scoped OTEL Collector, Prometheus, Tempo, Loki,
  Grafana, dashboards and namespaced network policy;
- revision 23: persisted the Prometheus numeric non-root identity fix;
- revision 24: allowed only the exact Coordinator and Specialist service
  accounts to export OTLP.

No observability ClusterRole or ClusterRoleBinding exists. No existing MCP
Deployment was rolled. All five observability pods are Ready with zero
restarts. The final smoke request `g3-edai2-otel-20261102-003` returned HTTP
200 and completed in 0.483 s with four Feast features, KServe model version 1,
fraud probability `0.0639895560144887`, and three RAG citations.

Tempo readback:

- trace ID: `11206d5194268089db547fc8bed734ae`;
- 79 spans and 77 linked parent-child edges;
- 37 spans from `fraud-coordinator`;
- 42 spans from `feature-rag-specialist`;
- root duration 365.39 ms;
- persisted trace and search readback both succeeded.

Prometheus readback includes one real invocation each of
`knowledge_search`, `feature_lookup`, and `fraud_scoring`. It also exposes
AgentGateway request outcomes and token totals and vLLM prompt/generation token
metrics. Prometheus, Loki, Tempo and Grafana health/readback passed. Grafana's
Prometheus and Loki datasource health endpoints returned `OK`; Grafana's Tempo
datasource health endpoint returned 404 even though direct Tempo readiness and
trace search/readback passed. The fallback vLLM scrape target remains down
(`EOF`); the authoritative primary vLLM target is up and served the smoke run.

Primary files: `observability/trace-summary.json`,
`observability/tempo-trace-11206d5194268089db547fc8bed734ae.json`,
`observability/prometheus-specialist-span-calls-final.json`,
`observability/prometheus-business-metrics-summary.json`,
`observability/grafana-datasource-health.txt`, and
`observability/final-runtime-summary.json`.

## F. Cloud safety, resources and rollback

- Existing GKE node count before/after: four; no node or node pool was added.
- Final node memory utilization: 26%, 33%, 44% and 67%.
- Added observability workload footprint at final readback: approximately 356
  MiB combined RSS and 28 millicores across Collector, Prometheus, Loki, Tempo
  and Grafana.
- A single new Grafana admin Secret was created for the new Grafana instance;
  no existing Secret value was read into evidence or changed.
- DataHub was local and bounded; its containers are stopped and volumes remain.
- Canary resources were deleted; production DB0 remained unchanged.
- Rollback boundary: `helm rollback llm-agent 21 --namespace default --wait
  --timeout 10m`, followed by readiness and G3 smoke verification. This was not
  needed.
- Billing reports were not reliable enough for a contemporaneous cumulative
  spend claim. No new billable node or managed service was created; all GKE
  work reused the existing four-node cluster. This is a resource observation,
  not a verified billing total.

## G. Tests

All final commands used checked-in project lock environments:

| Environment / command group | Result |
|---|---|
| `infra/docker/llm-agent-tooling/.venv/bin/python -m unittest ...` | 96/96 PASS |
| `infra/docker/g3-byo-coordinator/.venv/bin/python -m unittest ...` | 30/30 PASS |
| `infra/docker/rag-ingestion/.venv/bin/python -m unittest tests.llm_agent.test_rag_airflow_ingestion` | 29 PASS, 1 SKIP |
| `data_platform/metadata/datahub/.venv/bin/python -m pytest -q ...` | 19/19 PASS |
| `helm lint infra/helm/llm-agent-platform ...` | 1 chart, 0 failures |

The single skip is the opt-in heavyweight pinned embedding model test requiring
`EWALLET_RAG_MODEL_TESTS=1`; the actual Airflow-produced embeddings and DB1
runtime readback were verified separately. An initial diagnostic invocation
put the Specialist test in the tooling environment and failed to import `a2a`;
the same test then passed 30/30 in its correct locked G3 environment. This was
an environment-selection error, not a source regression.

Raw outputs are under `tests/`.

## H. Reproduction and cleanup

### RAG DB1 canary

The safe renderer/caller is `ops/llm_agent/rag_db1_canary.py`. It never renders
Secret values or the registry payload. Operators must read the immutable
registry and existing Secret inputs separately, render with digest-pinned image
and exact production authentication subjects, apply only objects carrying the
unique canary ownership label, call with the official MCP SDK, then delete by
that label. See:

```bash
infra/docker/llm-agent-tooling/.venv/bin/python \
  -m ops.llm_agent.rag_db1_canary --help
```

Expected final verdicts are `ISOLATED_READBACK_PASS`,
`MCP_CANARY_READ_AFTER_WRITE_PASS`, and
`PRODUCTION_DB0_CUTOVER_NOT_PERFORMED`.

### DataHub

```bash
export DATAHUB_PYTHON="$PWD/data_platform/metadata/datahub/.venv/bin/python"
DATAHUB_PULL_IMAGES=false \
  ./data_platform/metadata/datahub/runtime/start_datahub.sh start
./data_platform/metadata/datahub/runtime/start_datahub.sh check
DATAHUB_GMS_URL=http://127.0.0.1:8080 \
  infra/docker/rag-ingestion/.venv/bin/python \
  -m ops.llm_agent.rag_ingest datahub \
  --run-label '<read-back run label>' --datahub-mode publish
./data_platform/metadata/datahub/runtime/start_datahub.sh stop
```

Do not use the destructive DataHub `nuke` command. Expected terminal status is
`PUBLISHED_AND_READ_BACK` with 25 matching records.

### Observability and G3 smoke

Render/lint using `observability/override.yaml`, inspect the semantic diff,
upgrade the existing release, wait for readiness, and run
`scripts/llm_agent/g3_byo_specialist_smoke.py`. Query Tempo by trace ID and
Prometheus via its HTTP API. Expected proof is a persisted trace containing
both Coordinator and Specialist service spans plus non-zero MCP tool-call span
metrics. Temporary port-forwards used by this campaign were stopped.

## I. Rubric position

| Rubric area | Evidence-backed result | Limitation |
|---|---|---|
| EDAI-2 row 9 — RAG | Runtime RAG pipeline, immutable artifacts, Redis DB1 readback and production-equivalent authenticated MCP search are verified. | Production DB0 cutover was intentionally not authorized/performed. |
| EDAI-2 row 10 — governance/lineage | DataHub GMS actual publication/readback, Airflow-to-Feast lineage and five assertion run results are verified. | DataHub runtime was bounded local Quickstart, not a permanent cloud deployment. |
| Observability | Real Collector/backend persistence, correlated G3 trace, span-derived tool metrics, gateway/vLLM metrics and Grafana dashboards are verified. | Gateway/backend named spans and application log ingestion remain partial/uninstrumented; fallback vLLM scrape is down. |

These results support runtime evidence for the listed requirements but do not
claim an overall EDAI-2 score or replace the later screenshot/rubric evidence
review.

## J. Remaining gaps

1. Owner approval is still required before any production DB0 RAG cutover.
2. Instrument A2A Gateway, MCP Gateway, Feast and KServe as named trace services
   if end-to-end component-level spans are required.
3. Enable an application log pipeline only after a separate redaction and
   retention review; Loki health alone is not log-ingestion evidence.
4. Diagnose or deliberately disable the non-authoritative `vllm-fallback`
   scrape target. Primary serving is healthy.
5. Grafana Tempo datasource health API compatibility needs a focused check;
   direct Tempo backend readback already passes.
6. Write the Living Decision below to the canonical Google Doc after owner
   review. No historical decision was rewritten.

## K. Living Decision status

Google Docs was treated as read-only during execution. The copy-ready addendum
is in `LIVING_DECISION_COPY_READY.md`. No automatic canonical writeback was
attempted.
