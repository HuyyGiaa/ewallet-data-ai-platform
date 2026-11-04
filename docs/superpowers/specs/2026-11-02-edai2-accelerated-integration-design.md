# EDAI-2 Accelerated Integration Design

## Intent

Close the remaining EDAI-2 RAG, DataHub and observability runtime gaps without
changing the frozen G3 business path, production Redis DB0, G1/G2 serving,
shared security policy, IAM/RBAC or existing Secret values.

## Chosen design

1. Deploy a short-lived `rag-db1-canary` with the reviewed production MCP image,
   an exact run-scoped Feast registry snapshot and a derived DB1 connection
   Secret. Give it unique labels and narrowly scoped NetworkPolicies so the
   production MCP Service cannot select it. Exercise authenticated MCP
   `knowledge_search`, bind the response to the Airflow publication identity,
   then remove every canary resource.
2. Reuse the stopped, pinned DataHub 1.7.0.1 Quickstart volumes. Start only the
   existing containers in dependency order while enforcing a local-memory gate;
   publish the already generated 25 records with the existing fail-closed
   publisher and verify every aspect through the GMS API. Stop the runtime after
   evidence capture.
3. Reuse the existing OTEL/Prometheus/Loki/Tempo/Grafana chart. Add an explicit
   namespace-scoped observability mode which omits cluster RBAC and cluster
   metric discovery. Enable the collector and backends only after Helm semantic
   diff, resource and security gates. Correlate a real G3 smoke request with
   persisted/queryable telemetry; classify uninstrumented signals honestly.

## Invariants and rollback

- Work only on `codex/llm-g2-llmd-routing`; never advance canonical or main.
- Preserve Helm `llm-agent` revision 21 until the observability gate passes.
- Never change Coordinator/Specialist source, model/features, production DB0,
  MCP authentication, Gateway policy, IAM, cluster RBAC or node count.
- RAG rollback is deletion of owned canary resources. DataHub rollback is a
  non-destructive container stop. Observability rollback is Helm revision 21.
- Keep raw, redacted evidence with hashes; do not claim unavailable metrics,
  full production cutover or a successful readback without runtime proof.

## Acceptance

- `MCP_CANARY_READ_AFTER_WRITE_PASS` and
  `PRODUCTION_DB0_CUTOVER_NOT_PERFORMED`.
- DataHub GMS available with lineage and all quality assertion aspects read back.
- OTLP exporter connected, collector receives telemetry, a backend persists it,
  and a real G3 trace can be queried.
- Focused locked tests and negative security gates pass; temporary resources are
  removed and the frozen G3 health check remains green.
