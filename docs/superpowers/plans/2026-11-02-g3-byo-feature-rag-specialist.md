# G3 BYO Feature-RAG Specialist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace only the unreliable declarative Feature-RAG Specialist with a deterministic kagent 0.10.3 BYO Agent that performs genuine authenticated MCP tool calls and returns Coordinator-compatible evidence.

**Architecture:** Extract the narrow kagent BYO lifecycle helpers already proven by the Coordinator, then add a typed Specialist core using the official pinned MCP Streamable HTTP client. Keep Helm opt-in and build/deploy artifacts isolated so the only intended runtime resource change is `Agent/ewallet-agents/feature-rag-specialist`.

**Tech Stack:** Python 3.12, Pydantic 2.14, A2A SDK 0.3.26, kagent-core pinned to `8878c39`, MCP SDK 2.3.0, httpx/httpx2, Helm, Docker, Trivy, GKE.

**Spec:** `docs/superpowers/specs/2026-11-02-g3-byo-feature-rag-specialist-design.md`

## Global Constraints

- Preserve `fraud-coordinator`, Drift Specialist, both Gateways, kagent revision 2, MCP revision 3, ServiceAccounts, Secrets and security policies.
- Never call Feast/KServe/RAG directly and never execute pseudo-tool text.
- Use only `http://mcp-gateway.ewallet-agents.svc:8080/feature-rag/mcp` and the existing Specialist credential.
- Every production behavior change follows RED→GREEN TDD.
- Build/scan sequentially; do not create nodes or run G1/G2 benchmarks.
- Commit locally, but do not merge or push canonical/main.

## Review Focus

- An MCP response with `isError=false` but malformed/mismatched structured content must fail closed.
- Reordered, duplicated or unrequested tool execution must not be possible.
- A valid citation from another request/corpus cannot be silently accepted.
- Cancellation during a live MCP call must end the A2A task without a success artifact.
- Helm BYO enablement must not alter Coordinator, Drift, Gateway or policy resources.

---

### Task 1: Shared BYO runtime helpers

**Files:**
- Create: `ai_platform/llm_agent/byo_runtime.py`
- Modify: `ai_platform/llm_agent/byo_coordinator.py`
- Modify: `tests/llm_agent/test_g3_byo_coordinator.py`

**Interfaces:**
- Produces: `ControllerTokenHook(app_name: str, token_path: Path)`, `AppResources`, `controller_task_store(app_name: str)`, `configure_telemetry(service_name: str, service_version: str)`.
- Preserves: existing Coordinator public app and lifecycle behavior byte-for-behavior.

- [ ] Add tests that import shared helpers, verify the Coordinator uses its own app name, rotated token reads, and unchanged protocol behavior; run and observe RED because the module does not exist.
- [ ] Extract the minimal helpers and update Coordinator imports without changing orchestration logic.
- [ ] Run BYO Coordinator and Programmatic Coordinator suites; expect 32/32 PASS.
- [ ] Commit `refactor(g3): share BYO agent runtime helpers`.

### Task 2: Typed MCP client and BYO Specialist A2A server

**Files:**
- Create: `ai_platform/llm_agent/byo_feature_rag_specialist.py`
- Create: `tests/llm_agent/test_g3_byo_feature_rag_specialist.py`
- Modify: `infra/docker/g3-byo-coordinator/pyproject.toml`
- Modify: `infra/docker/g3-byo-coordinator/uv.lock`

**Interfaces:**
- Consumes: shared TaskStore/controller/telemetry helpers from Task 1 and `G3BusinessRequest`.
- Produces: `FeatureRagMCPClient.run(request) -> SpecialistEvidence`, `FeatureRagSpecialistExecutor`, `build_app()` and module entrypoint.

- [ ] Add failing tests for valid ordered calls, exact Gateway endpoint/header, initialize/list validation, missing tool, auth/error response, malformed content, transaction/prediction mismatch, missing citations, timeout, cancellation, idempotency/session isolation and actual local A2A JSON-RPC.
- [ ] Run the new suite and observe RED because the Specialist module is absent.
- [ ] Add MCP 2.3.0 to the locked project and implement the smallest async Streamable HTTP client plus typed validators.
- [ ] Implement the official A2A executor/server using shared runtime helpers and bounded task lifecycle.
- [ ] Run the Specialist, Coordinator and Programmatic suites; expect all PASS.
- [ ] Commit `feat(g3): add deterministic BYO Feature-RAG Specialist`.

### Task 3: Image, Helm, smoke and rollback contract

**Files:**
- Modify: `infra/docker/g3-byo-coordinator/Dockerfile`
- Create: `scripts/llm_agent/build_g3_byo_feature_rag_specialist.sh`
- Create: `scripts/llm_agent/g3_byo_specialist_smoke.py`
- Modify: `infra/helm/llm-agent-platform/templates/agents.yaml`
- Modify: `infra/helm/llm-agent-platform/values.yaml`
- Modify: `tests/llm_agent/test_agents_chart.py`
- Create: `docs/llm_agent/g3_byo_feature_rag_specialist_runbook.md`

**Interfaces:**
- Consumes: Specialist module and locked environment from Task 2.
- Produces: private digest-only image contract and opt-in Helm values for only `feature-rag-specialist`.

- [ ] Add RED chart/image tests proving only the Specialist Agent changes, private digest pinning, same ServiceAccount/resources/placement, exact Secret key injection and unchanged Coordinator.
- [ ] Add Specialist image entrypoint/build script and runtime import smoke; add a bounded A2A smoke that never logs credentials.
- [ ] Add Helm opt-in and runbook; render against current live values.
- [ ] Run chart/G3/MCP/A2A locked suites, `uv lock --check`, `uv sync --locked`, compile and `git diff --check`; expect PASS.
- [ ] Commit `feat(g3): package BYO Feature-RAG Specialist rollout`.

### Task 4: Controlled build, rollout, E2E and evidence

**Files:**
- Create: `docs/evidence/edai2/g3_byo_feature_rag_specialist_<timestamp>/...`

**Interfaces:**
- Consumes: committed source SHA, image/build script and Helm opt-in.
- Produces: immutable remote digest, Helm revision, real end-to-end evidence and SHA256 manifest.

- [ ] Read back source, releases, Agent conditions, node/RAM capacity and rollback baseline; stop if any mandatory gate changed.
- [ ] Build, import-smoke, scan and push one Specialist image sequentially; verify remote digest.
- [ ] Render candidate from live values and prove semantic diff is limited to the Feature-RAG Agent configuration/directly owned runtime contract.
- [ ] Upgrade `llm-agent`, verify Agent/Deployment/Service, digest, ServiceAccount, Agent Card, Coordinator/Drift health and MCP connectivity.
- [ ] Run one bounded full Coordinator E2E and invalid/credential/bypass negative tests; record trace/task/tool-call/backend values without secrets.
- [ ] Roll back only `llm-agent` on deployment/security regression; otherwise preserve the validated revision.
- [ ] Write Vietnamese evidence report, copy-ready Living Decision and SHA256 manifest; commit evidence.
- [ ] Run fresh final verification and report every gate without unsupported PASS claims.
