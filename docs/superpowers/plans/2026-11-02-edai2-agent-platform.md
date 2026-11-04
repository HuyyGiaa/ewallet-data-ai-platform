# EDAI-2 Agent Platform implementation plan

> Execute in the existing `codex/llm-g2-llmd-routing` worktree. Preserve the
> untracked G3 evidence directories and do not touch canonical/main.

**Goal:** Close the authorized source gaps for kagent registry validation and
bounded Agent autoscaling, while producing a reproducible, approval-gated
Sandbox/Substrate package and honest runtime evidence classification.

**Architecture:** Kubernetes Agent CRDs remain the registry. Native HPA scales
kagent-owned Agent Deployments; KEDA remains limited to Substrate WorkerPools.
Controller-backed task storage preserves A2A task reads across replicas.
SandboxAgent remains isolated from production and cannot be deployed until the
Substrate privileged infrastructure gate is approved.

**Stack:** Helm 3, Kubernetes autoscaling/v2 and policy/v1, kagent 0.10.3,
Agent Substrate 0.0.9, Python 3.12, pytest/unittest, JSON Schema.

---

### Task 1: Establish the fail-closed registry/lifecycle probe

**Files:**
- Create: `scripts/llm_agent/agent_platform_probe.py`
- Create: `tests/llm_agent/test_agent_platform_probe.py`

1. Write failing tests for complete inventory, missing readiness, mismatched
   ownership, Agent Card capability mismatch and secret redaction.
2. Implement a read-only probe over captured Kubernetes/API JSON inputs.
3. Run the focused probe tests and Python compilation.

### Task 2: Add supported Agent/BYO HPA ownership

**Files:**
- Modify: `infra/helm/llm-agent-platform/templates/agents.yaml`
- Modify: `infra/helm/llm-agent-platform/templates/keda.yaml`
- Modify: `infra/helm/llm-agent-platform/values.yaml`
- Modify: `infra/helm/llm-agent-platform/g3/g3.yaml`
- Modify: `tests/llm_agent/test_agents_chart.py`

1. Add failing render tests proving Agent mode emits HPA/PDB, omits CR replica
   ownership, uses exact Deployment targets and stays bounded.
2. Add failing tests proving Sandbox mode retains WorkerPool/KEDA semantics and
   invalid mode/signal combinations fail closed.
3. Implement the minimum Helm changes.
4. Render both modes, validate schemas and run focused chart tests.

### Task 3: Close Sandbox/Substrate source and approval documentation

**Files:**
- Modify: `infra/kubernetes/kagent-pilot/README.md` (create if absent)
- Modify: `docs/llm_agent/agents.md`
- Create: `docs/llm_agent/agent_platform_20261102.md`
- Modify: `tests/unit/test_kagent_bounded_pilot.py`

1. Test the pinned versions, isolation boundaries, no production Secret/tool
   references, resource bounds and cleanup/render contract.
2. Render all four pinned OCI charts and verify digests and CRD schemas.
3. Document exact privileged/cluster-scoped resources and the separate owner
   approval required before runtime installation.

### Task 4: Local integration and evidence

**Files:**
- Create: `docs/evidence/llm_agent/agent_platform_20261102/<artifacts>`
- Modify: `docs/llm_agent/living_decision_drafts.md`

1. Run focused tests, Helm lint/render, schema validation and local read-only
   CRD/API probes.
2. Attempt bounded GKE readback with explicit context and timeout. Preserve the
   exact authorized-network blocker if it remains.
3. Generate SHA256SUMS and a PASS/FAIL/SKIP/BLOCKED matrix. Never promote a
   source-only check to runtime VERIFIED.

### Task 5: Final verification and atomic commits

1. Run the relevant combined LLM/Agent regression group.
2. Run `git diff --check`, compilation and secret/payload scans.
3. Verify the two pre-existing untracked evidence directories remain unadded.
4. Commit design, implementation and evidence in small atomic commits. Do not
   merge or push.

