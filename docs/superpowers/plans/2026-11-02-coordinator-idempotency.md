# Coordinator Durable Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent cross-context and cross-replica replay of a completed G3 business request, restore safe Agent HPA, then continue the approved pinned Substrate pilot.

**Architecture:** A dedicated authenticated Redis StatefulSet is the shared authority for scoped business request records. Atomic Lua transitions implement claim, started, completion, replay, conflict and ambiguous-crash behavior; the Coordinator fails closed when state cannot be proven. Helm adds only the isolated Redis resources, Coordinator wiring and the previously reviewed HPA/PDB candidate.

**Tech Stack:** Python 3.12, redis-py with locked `uv.lock`, Redis Lua, Helm, Kubernetes StatefulSet/PVC/NetworkPolicy, kagent 0.10.3, Agent Substrate 0.0.9.

**Spec:** `docs/superpowers/specs/2026-11-02-coordinator-idempotency-design.md`

## Global Constraints

- Preserve G3 Agent Cards, Gateways, Secrets, Feast, KServe, RAG and model contracts.
- Scope keys by trusted tenant, Istio-authorized service principal and business request ID.
- Never use SDK context ID or process-local memory as the production authority.
- Never automatically retry a stale record after downstream execution may have started.
- Dedicated Redis must not share credentials, data or lifecycle with Feast/RAG Redis.
- No new nodes, node pools, broad IAM/RBAC, public ingress, merge or push.
- Runtime claims require real GKE evidence; rollback on any critical regression.

## Review Focus

- A Redis response that is malformed or unexpectedly typed must fail closed, not become a new claim.
- A completed result larger than the configured byte budget must not be stored or replayed.
- A stale owner completing after generation change must be rejected without altering the newer record.
- An inbound forwarded identity header must not replace the source-configured authenticated service principal.
- An `AMBIGUOUS` record must survive ordinary expiry cleanup until explicit operator reconciliation.

---

### Task 1: Shared idempotency state machine

**Files:**
- Create: `ai_platform/llm_agent/idempotency.py`
- Create: `tests/llm_agent/test_g3_durable_idempotency.py`

**Interfaces:**
- Produces: `IdempotencyScope(tenant: str, principal: str)`.
- Produces: `Claim(owner_token: str, generation: int)` and typed claim outcomes.
- Produces: synchronous `IdempotencyStore` protocol with `claim`, `read`, `mark_started`, `complete`, and `release_unstarted` operations.
- Produces: `InMemoryIdempotencyStore` only for deterministic tests/local injection, never the production factory.

- [ ] Write failing tests for same ID across contexts/replicas, concurrent identical claims, completed replay, conflicting payload, scope isolation, bounded result size, safe pre-start reclaim, post-start ambiguity and stale-owner rejection.
- [ ] Run `python3 -m unittest -v tests.llm_agent.test_g3_durable_idempotency` and verify RED because the module does not exist.
- [ ] Implement the smallest typed state machine and test store with explicit lease/TTL behavior.
- [ ] Re-run the focused test and verify PASS.
- [ ] Commit Task 1 source and tests.

### Task 2: Redis authority and Coordinator integration

**Files:**
- Create: `ai_platform/llm_agent/redis_idempotency.py`
- Modify: `ai_platform/llm_agent/programmatic_coordinator.py`
- Modify: `ai_platform/llm_agent/byo_coordinator.py`
- Modify: `infra/docker/g3-byo-coordinator/pyproject.toml`
- Modify: `infra/docker/g3-byo-coordinator/uv.lock`
- Modify: `tests/llm_agent/test_g3_durable_idempotency.py`
- Modify: `tests/llm_agent/test_g3_programmatic_coordinator.py`
- Modify: `tests/llm_agent/test_g3_byo_coordinator.py`

**Interfaces:**
- Consumes: Task 1 store protocol and scope/claim types.
- Produces: `RedisIdempotencyStore.from_url(...)` using Lua transitions and Redis server time.
- Produces: `idempotency_store_from_env()` that requires the dedicated Redis URL in cluster mode and never falls back to memory.
- Produces: `ProgrammaticCoordinator.handle(..., scope: IdempotencyScope, ...)` independent of SDK context ID.

- [ ] Extend tests first for Redis outage/malformed reply, two Coordinator instances sharing one store, bounded duplicate waiting, no second transport call, payload conflict, and trusted fixed runtime scope despite spoofed forwarded identity.
- [ ] Run the focused Coordinator suite and verify RED for the missing durable integration.
- [ ] Add the pinned Redis dependency with `uv`, implement atomic Lua claim/start/complete/release scripts, and wire the production factory.
- [ ] Make ambiguous post-start failures persistent and fail closed; do not claim exactly-once in the crash window.
- [ ] Run locked Coordinator tests and verify PASS.
- [ ] Commit Task 2 source, lockfile and tests.

### Task 3: Dedicated Redis Helm resources

**Files:**
- Create: `infra/helm/llm-agent-platform/templates/idempotency-redis.yaml`
- Modify: `infra/helm/llm-agent-platform/templates/agents.yaml`
- Modify: `infra/helm/llm-agent-platform/values.yaml`
- Modify: `infra/helm/llm-agent-platform/g3/g3.yaml`
- Modify: `tests/llm_agent/test_agents_chart.py`
- Modify: `tests/llm_agent/test_g3_runtime_config.py`

**Interfaces:**
- Consumes: Task 2 environment variables and Secret key names.
- Produces: one pinned Redis StatefulSet, ClusterIP Service, PVC template and exact ingress/egress NetworkPolicies.
- Produces: Coordinator Secret reference plus immutable tenant/principal, lease, wait and TTL settings.

- [ ] Write Helm contract tests first for image digest, AOF, PVC, probes, non-root security, resource bounds, no ingress, Secret references, exact network identities and absence from Feast/RAG resources.
- [ ] Run chart tests and verify RED because the resources are missing.
- [ ] Implement the minimal templates and values; require a pre-created Secret and never render credential material.
- [ ] Render/lint all G3 profiles, inspect semantic diff and verify only approved resources/configuration change.
- [ ] Commit Task 3 source and tests.

### Task 4: Build and one-replica runtime validation

**Files:**
- Create: `docs/evidence/llm_agent/agent_idempotency_runtime_<timestamp>/...`

**Interfaces:**
- Consumes: Tasks 1-3 and the existing image build/push procedure.
- Produces: immutable Coordinator image digest and one-replica Redis/idempotency runtime evidence.

- [ ] Re-read Git, public IP, Authorized Networks, Helm ownership, node/storage headroom and G3 baseline; add only the verified temporary `/32`.
- [ ] Generate a random dedicated credential without logging it; create only the approved Secret.
- [ ] Deploy Redis and prove auth denial, persistence/AOF, PVC binding, NetworkPolicy isolation, probes and independent lifecycle.
- [ ] Build/scan/push the changed Coordinator image once and verify the remote digest.
- [ ] Inspect the Helm semantic diff, deploy only Redis and Coordinator wiring/image, then verify one-replica G3 and replay behavior with actual MCP invocation counts.
- [ ] Roll back the affected release on any critical gate failure; preserve Redis PVC for recoverable investigation.

### Task 5: HPA runtime closure

**Files:**
- Extend: `docs/evidence/llm_agent/agent_idempotency_runtime_<timestamp>/...`

**Interfaces:**
- Consumes: validated one-replica runtime and prior HPA-only values.
- Produces: real scale-out/in, cross-replica replay and G3/security evidence.

- [ ] Deploy the reviewed 3 HPA + 3 PDB candidate without Agent Card drift.
- [ ] Verify two Ready replicas per Agent, then bounded load causes 2→3 scale-out.
- [ ] Send identical and concurrent requests through distinct A2A contexts/replicas; prove one active owner and no additional Specialist/MCP/Feast/KServe execution after completion.
- [ ] Wait through the configured stabilization window and verify 3→2 scale-in.
- [ ] Re-run G3 Business E2E, MCP least privilege, direct-bypass denial and backend health.
- [ ] Roll back if any idempotency, security, readiness or business gate fails.

### Task 6: Approved Substrate pilot and closure

**Files:**
- Extend: `docs/evidence/llm_agent/agent_idempotency_runtime_<timestamp>/...`

**Interfaces:**
- Consumes: successful Task 5 runtime and the existing pinned Substrate 0.0.9 pilot.
- Produces: WorkerPool/Actor/isolation lifecycle evidence or one exact independent blocker.

- [ ] Revalidate the pinned OCI identities, CRD ownership, privileged DaemonSet/hostPath, admission scope, capacity and rollback plan.
- [ ] If every gate passes, deploy the exact reviewed pilot and demonstrate WorkerPool, SandboxAgent, Actor execution, isolation negatives and supported suspend/snapshot/resume behavior.
- [ ] Run the complete locked regression suite, `git diff --check`, Helm validation and evidence checksum verification.
- [ ] Restore the original GKE Authorized Networks list.
- [ ] Commit only verified source/evidence; leave unrelated evidence directories untouched and do not merge/push.
