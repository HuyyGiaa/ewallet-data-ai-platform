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

## Current phase

Phase A foundation is implemented on `codex/llm-agent-rubric-20261030` from the
reviewed consolidation base `acad36beb81a8424db6ee0a3a04905dbb040ce0d`.
No cloud, cluster, paid workload, registry, canonical branch, active DP2, dirty
Feast worktree, or dirty Streaming worktree was mutated.

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
- Phase B next implements vLLM CPU-first plus agentgateway routing, resilience,
  version routing and benchmark harnesses. It remains source-only until an
  explicitly approved deployment and real benchmark are captured.
