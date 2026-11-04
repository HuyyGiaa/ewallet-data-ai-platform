# G3 offline source checkpoint

Status: **SOURCE_READY, NOT RUNTIME_VERIFIED.** This checkpoint is based on
committed G2 `0c89af3b08561e4f9e58b7cc80273457ab6e4d0f` and selectively ports I7
`50e9e7333b852bb4ab71da7d766c8584b815c5c2`. It creates no Secret and performs
no cluster or cloud operation.

## Architecture and preserved boundaries

The Coordinator and two Specialists use the existing shared
`ewallet-global-llm` ModelConfig and native kagent A2A. No model server, A2A
gateway, frontend or orchestration runtime was added. `CoordinatorOrchestrator`
is a transport-neutral typed planner/aggregator used to test the structured
A2A contract; the deployed transport remains kagent 0.10.3.

The authoritative grants remain:

- `drift-specialist`: `feature_lookup`, `drift_detection`;
- `feature-rag-specialist`: `feature_lookup`, `fraud_scoring`,
  `knowledge_search`;
- Coordinator: native Agent delegation only, no MCP business tools.

G1/G2 vLLM, C1 settings, agentgateway, llm-d, benchmark overlays, resource
limits, Gateway API, Istio and NetworkPolicy behavior are unchanged. The
optional I7 observability stack is disabled by default.

## MCP production authentication

`ServiceTokenAuthenticator` extends the existing `Authenticator` boundary.
Each MCP service loads only its own JSON credential table from
`EWALLET_MCP_SERVICE_TOKENS`, populated by a Kubernetes Secret reference. A
credential maps a token to a server-verified known Specialist subject. Tokens
are compared in constant time. Missing, malformed, unknown, revoked or
all-revoked configuration selects `DenyAllAuthenticator`; logs contain only a
fixed rejection code.

kagent sends `X-Ewallet-MCP-Service-Token` from a per-Agent, per-MCP-server
Secret reference using `headersFrom`. `ToolGrants` then applies to the verified
Principal for REST, MCP `tools/list` and MCP `tools/call`. Caller actor headers
do not establish identity. `values-g3-auth-example.yaml` contains references
only and deliberately contains no credential value.

Rotation is overlap-first and service-local: add a new active credential,
roll the corresponding Agent Secret reference, verify negative/positive calls,
then mark the old credential revoked and roll the server. Never log, render,
commit or place the token directly in Helm values.

## I7 compatibility ports

The port retains I7 ToolTelemetry, semantic MCP outcomes, safe structured
events, OpenTelemetry spans and exporter-failure isolation. It also retains
MiniLM ONNX embedding contracts, separate RAG Feast publication/readback,
exact retrieval with provenance, `chunk_lookup`, DataHub lineage models and
the uv-locked RAG ingestion runtime. The original G2 manual source-only DAG
remains unchanged; the I7 publication DAG is added as
`rag_fraud_policy_ingestion` with its own parameter module, avoiding a silent
change to the G2 gate.

The production feature lookup and fraud scoring adapters remain the existing
G2 Feast/KServe integrations. No local SQLite result is evidence for shared
GKE Redis, KServe or DataHub.

## Runtime gates

Before enabling G3 in GKE, an operator must:

1. create service-specific credential tables and Agent header Secrets outside
   Git; verify rotation and revocation without reading values into evidence;
2. render/apply with the pinned kagent 0.10.3 CRDs and prove Secret forwarding;
3. prove MCP HTTP positive and negative authentication through the existing
   MCP Gateway while preserving Istio/NetworkPolicy boundaries;
4. register Coordinator and Specialists, then capture native A2A delegation,
   partial-failure and unauthorized-tool traces;
5. verify the separate RAG Feast registry against shared Redis, MiniLM model
   artifact, DataHub publication/readback, Feast feature lookup and registered
   KServe/Fraud API scoring;
6. run resource/cost preflight and the bounded business E2E suite; and
7. validate Prometheus/Loki/Tempo/Grafana exports and redaction in the target
   environment.

Until every applicable item passes, report these capabilities as source-ready
only, never deployed or runtime-verified.
