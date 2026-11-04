# LLM and Agent implementation handoff

Last updated: 2026-10-30 (Asia/Ho_Chi_Minh)

## Architecture precedence

Living Decision 21.216 is authoritative for this campaign:

- external ingress is Istio Ambient plus Kubernetes Gateway API, not NGINX;
- agent delegation is kagent native A2A, with no standalone A2A gateway;
- agentgateway owns LLM model/backend routing;
- the MCP Gateway is a policy-only proxy;
- runtime and rubric items require real deployment/readback evidence and cannot
  become VERIFIED from source, configuration, or tests alone.

## Completed source milestones

Phase A foundation is implemented on `codex/llm-agent-rubric-20261030` from the
reviewed consolidation base `acad36beb81a8424db6ee0a3a04905dbb040ce0d`.
The implementation commit is `5fd25ebe8e32b4b18d86b9be07f4aece8cd0d30a`;
the reused fail-closed rubric commit is
`0ae2f43a9de4d633d1ac6d30b5440f0549a1f5ea`.
No cloud, cluster, paid workload, registry, canonical branch, active DP2, dirty
Feast worktree, or dirty Streaming worktree was mutated.

Phase B source implementation is commit
`1c3d8997ffed13009f67ca9e0c354467b51ab6dd`. It adds pinned vLLM CPU primary
and fallback backends, agentgateway 1.7.0 model routing and resilience, a
kagent 0.10.3-compatible global ModelConfig, fail-closed benchmark evidence and
an actual streaming benchmark runner. Runtime deployment and benchmark remain
OPEN/PARTIAL, never VERIFIED.

Phase C source implementation is commit
`8dc1c54f3f4fd8a1db89195e733a52524562a034`. It adds an immutable fraud-policy
corpus, checksum validation, bounded chunking, a real embedding-endpoint client,
Feast 0.66 publication definitions, exact source-artifact retrieval, citations,
retrieval evaluation, a manual Airflow DAG and a DataHub lineage plan. Redis is
explicitly publication-only rather than falsely treated as a vector backend.
Airflow, embedding, Feast and DataHub runtime proof remain OPEN/PARTIAL.

## Gate A source contract

The gate covers typed transport-neutral contracts, a versioned deployable-unit
inventory, Helm dev/GCP overlays, Ambient namespace, Gateway API route,
least-privilege service accounts, deny-by-default Istio/Kubernetes policies,
a pinned multi-stage validation image, changed-component detection, and a
Jenkins-compatible immutable-SHA pipeline skeleton.

`python3 -m ops.llm_agent.preflight` must report `SOURCE_ONLY_PASS` and
`runtime_verified: false`. A successful render is not deployment evidence.

## Runtime blockers and next gate

- DP2 remains active; do not duplicate a paid trigger or mutate its checkout.
- Feast Online/Redis, registered ML model/KServe and runtime agent prerequisites
  are not assumed complete.
- Google Docs milestone updates require successful save/readback.
- Phase D next implements the Feature/RAG/Fraud Evidence API and Drift Detection
  API behind the policy-only MCP Gateway, preserving the four approved MCP tool
  names and existing ML feature/serving contracts.
