# Living Decision addendum — EDAI-2 bounded RAG, DataHub and Observability closure

**Date:** 2026-11-02
**Status:** Accepted for owner review; canonical Google Doc writeback pending
**Source branch:** `codex/llm-g2-llmd-routing`
**Initial source HEAD:** `34494d44299f08a7751af66b86f6af55e0b9d5fe`
**Implementation HEAD:** `9d6ab73ef9015b61ac5588b1eb4760b5dfe17362`

The owner-approved accelerated EDAI-2 campaign closed three bounded runtime
gates while preserving the existing G3 business path and G1/G2 serving.

1. RAG read-after-write is verified from the exact successful Airflow run
   `g3-functional-rag-20261102t045700z`. A short-lived, authenticated MCP
   canary reused the production image digest and immutable registry, read four
   deterministic chunks from Redis DB1, and returned their document IDs,
   checksums and publication identity through real `knowledge_search`.
   Verdicts: `ISOLATED_READBACK_PASS` and
   `MCP_CANARY_READ_AFTER_WRITE_PASS`. Production Redis DB0 was not changed;
   `PRODUCTION_DB0_CUTOVER_NOT_PERFORMED` remains explicit. All canary resources
   were deleted.
2. The existing bounded local DataHub 1.7.0.1 runtime published and read back
   25/25 records covering source documents, Feast schema/dataset, Airflow job
   lineage and five quality assertions with passing run results. Frontend and
   Actions were not required; persistent volumes were preserved and all
   started containers were stopped after verification.
3. The existing GKE cluster now runs a namespace-scoped observability stack at
   Helm `llm-agent` revision 24. No observability ClusterRole or
   ClusterRoleBinding was introduced. OTEL Collector, Prometheus, Tempo, Loki
   and Grafana are Ready. A real G3 request was persisted and read back from
   Tempo as trace `11206d5194268089db547fc8bed734ae` with 79 spans across
   Coordinator and Feature-RAG Specialist. Prometheus readback proves
   `knowledge_search`, `feature_lookup` and `fraud_scoring` tool spans plus
   AgentGateway/vLLM request and token metrics.
4. The stable synthetic business path remained functional: real Feast feature
   values, KServe model version 1 and bound RAG citations returned in 0.483 s.
   Core G3 pods were Ready with zero restarts. No Coordinator/Specialist
   business source, model, feature contract, Gateway authentication, IAM,
   cluster-wide RBAC, existing Secret value, node pool, canonical branch or
   main branch was modified.
5. Final locked regression results were 96/96 tooling tests, 30/30 G3 BYO
   tests, 29 RAG ingestion passes plus one explicitly opt-in heavy-model skip,
   19/19 DataHub tests, and Helm lint with zero failures.

Remaining limitations are intentionally not promoted to PASS: production DB0
cutover still requires separate approval; A2A Gateway, MCP Gateway, Feast and
KServe are not separate named services in the captured trace; application log
ingestion is disabled; the non-authoritative vLLM fallback scrape is down; and
Grafana's Tempo datasource health API returns 404 although direct Tempo
readiness, search and trace readback pass. Billing-report latency prevented a
verified cumulative-cost statement; no new node or managed service was
created.

Evidence is versioned under
`docs/evidence/llm_agent/edai2_integration_20261102/`, with SHA-256 checksums.
This addendum records runtime evidence only and does not declare an overall
EDAI-2 score. Historical Living Decisions remain unchanged.
