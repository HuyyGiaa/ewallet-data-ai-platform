# Proposal: Drift Specialist system prompt from agentregistry (`llm-agent` r32 → r33)

Status: **prepared and canary-validated on kind; production NOT approved, nothing applied.** Final security verdict and exact rollout: §8 (2026-11-03).

## 1. Mechanism

The Drift Specialist is a kagent declarative agent. Its golang-adk image cannot call the registry itself. The prompt therefore reaches it through this approved initialization path:

```text
agentregistry (ewallet-prompts, Prompt drift-specialist-system:<version>)
  → ops/llm_agent/prompt_registry.py configmap
      (explicit version; sha256 must equal prompts/manifest.json; fail-closed)
  → ConfigMap ewallet-agents/drift-specialist-prompt-managed
      (annotations: prompt-version, prompt-sha256, prompt-source)
  → kagent controller (watches the ConfigMap, regenerates the agent config Secret, config-hash)
  → drift-specialist pod
```

## 2. Exact source change (commit on `codex/llm-g2-llmd-routing`)

`infra/helm/llm-agent-platform/templates/agents.yaml` gains an optional per-agent `promptSource: git | registry`, defaulting to `git`. Any other value fails the render.

With `registry`, the Agent changes in two ways:
- It reads `systemMessageFrom: <agent>-prompt-managed`.
- It carries `ewallet.io/prompt-source: registry`.

The chart keeps rendering the Git ConfigMap `<agent>-prompt-<version>` (for audit and as the rollback target) and never renders the managed ConfigMap.

Tests in `tests/llm_agent/test_agents_chart.py`:
- the registry source points to the managed ConfigMap
- the Git copy is still rendered
- the managed ConfigMap is not chart-owned
- other agents are unchanged
- explicit `git` renders identically to the default
- an unknown value fails

## 3. Helm semantic diff (computed 2026-11-03 against the live release)

| Check | Result |
|---|---|
| Current source with live r32 values vs live r32 manifest | 185 objects, 0 added, 0 removed, 0 fields changed |
| r33 values = r32 values + `agents.specialists.drift-specialist.promptSource: registry` | 0 added, 0 removed |
| `Agent/ewallet-agents/drift-specialist` `metadata.annotations["ewallet.io/prompt-source"]` | absent → `registry` |
| `Agent/ewallet-agents/drift-specialist` `spec.declarative.systemMessageFrom.name` | `drift-specialist-prompt-v2` → `drift-specialist-prompt-managed` |

Unchanged:
- Coordinator and Feature-RAG Specialist
- MCP servers, tool schemas and authorization
- Gateways and their policies
- ModelConfig
- HPA, PDB
- Sandbox and Substrate
- ML feature contract

## 4. Canary evidence (kind, `docs/evidence/edai2/cicd_runtime_20261103/prompt-canary-kind/`)

The canary is a drift-specialist twin with the same declarative settings and the same pinned kagent 0.10.3 controller and go-agent image digests as production. Its LLM is a mock (`prompt-echo`) that logs the exact system message it receives.

| Test | Result |
|---|---|
| Actual consumption | The system message sent to the LLM starts with registry `drift-specialist-system/v2` verbatim (sha `0c0798e1…`). The only addition is kagent's identity line. The reply is `prompt_revision: drift-specialist/v2`. |
| Explicit version switch | Rendering v1 from the registry changed the config-hash and rolled the pod. The LLM then received v1 (`9330dda3…`) and the reply was `drift-specialist/v1`. |
| Tampered registry content | The registry accepted an overwrite. The resolver refused it on the sha256 mismatch; ConfigMap and config-hash unchanged. |
| Unknown version (v9) | Refused before any call. |
| Registry unavailable | Without fallback: refused. With `--allow-fallback`: the ConfigMap content is identical (`0c0798e1…`), annotated `pinned-git-fallback`, and the config-hash is unchanged, so the effective prompt does not change. |
| r32 → r33 → r32 → r33 transition | config-hash `12191494…` constant; **no pod restart** at any step (identical content). |
| Contract regression in the r33 state | 3/3 A2A replies `contract_version: g3-orchestration/v1`, `prompt_revision: drift-specialist/v2`. |

Limits of the canary:
- It has no MCP tools, no Feast, KServe or RAG, and no Coordinator.
- The full G3 business path cannot run on kind.
- Production G3 regression PASSED on 2026-11-03 against r32. Since r33 changes no G3 path object, it must be re-run after rollout.

## 5. Production rollout plan (requires owner approval)

1. Pre-checks:
   - G3 regression on r32 (business E2E, replay idempotency, MCP 401/authorization).
   - Drift identity probe returns `drift-specialist/v2`.
   - Record the config-hash and pod name.
2. Operator only, through a port-forward to `ewallet-prompts`:
   - `python -m ops.llm_agent.prompt_registry configmap --agent drift-specialist --version v2 --name drift-specialist-prompt-managed --namespace ewallet-agents`, then `kubectl apply`.
   - Verify `prompt-source=registry` and sha `0c0798e1…`.
3. `helm upgrade llm-agent -n default -f r33-values.yaml --wait`. The live semantic diff must show exactly the 2 fields in §3.
4. Post-checks:
   - Agent Ready.
   - config-hash and pod unchanged (expected).
   - Drift identity probe returns `drift-specialist/v2`.
   - G3 regression.
   - Unrelated objects unchanged (`helm get manifest` diff).

**Rollback:** `helm rollback llm-agent 32 -n default --wait`, which restores `systemMessageFrom: drift-specialist-prompt-v2` (the Git copy is still present). The managed ConfigMap may be kept or deleted; it is not chart-owned.

## 6. Open risks and gaps

- **No authentication on registry writes.** agentregistry 0.4.0 accepted an unauthenticated `/v0/apply` in this configuration. Exposure is limited by:
  - default-deny NetworkPolicy plus same-namespace ingress in `ewallet-prompts`
  - RBAC on `pods/portforward`
  Consumers are protected by the resolver's sha256 check against the Git manifest. Enabling registry auth (`ARCTL_API_TOKEN`) is a follow-up.
- **No resolver CD yet.** Prompt changes are applied by the operator. A Jenkins prompt CD would additionally need a narrow `configmaps` update on `drift-specialist-prompt-managed` in `ewallet-agents` and a registry path that needs no kubectl. That is a separate proposal.
- **No Langfuse.** agentregistry is the rubric-linked tool used instead. Whether the reviewer accepts it as "a tool such as Langfuse" is an acceptance risk.

## 7. Readiness after the security assessment (2026-11-03)

Security assessment: `docs/edai2/prompt_registry_security_assessment_20261103.md`.

Registry writes are unauthenticated in agentregistry 0.4.0 (no auth option exists). This was demonstrated live by an executor probe error (F9): the registry was tampered, then restored. They cannot change the effective prompt, because the resolver enforces the Git manifest sha256 fail-closed. `verify-all` detected the tampering immediately.

**r33 production readiness: NOT YET REQUESTED.** Gates before asking for approval:

1. Owner decision on M1 (remove Jenkins port-forward in `ewallet-prompts`) and M2 (pod-specific NetworkPolicy). Both are server-dry-run validated (`registry-security/m1-m2-server-dry-run.txt`).
2. `verify-all` = 12/12 MATCH immediately before step 2 of §5 (current state after restore: 12/12).
3. The rollout plan in §5 is unchanged:
   - exact diff of 2 fields
   - runtime prompt `drift-specialist/v2`, sha `0c0798e185cd5f86734069b7688badd0ab0aea2a416cab29f62767d8e4375fef`
   - G3 regression before and after
   - rollback `helm rollback llm-agent 32`

## 8. Final security verdict and exact rollout proposal (owner decision 7, 2026-11-03)

**r33 remains NOT APPROVED.** Nothing in this section has been executed. The Drift Specialist production release is unchanged: `llm-agent` r32, `systemMessageFrom: drift-specialist-prompt-v2`.

### 8.1 Security verdict

**Ready to request approval; no security blocker.**

| Control | State | Effect on r33 |
|---|---|---|
| Integrity anchor | The Git `prompts/manifest.json` sha256 is enforced by the resolver | A registry write cannot change the effective prompt: proven by the kind canary and by incident F9 on GKE |
| M3 gate | Implemented: `prompt_registry configmap` refuses unless all 12 pinned versions MATCH | Step 2 below cannot render from a tampered registry, even for an untouched version |
| M1 | **Applied and verified**: Jenkins port-forward is Forbidden | The only non-human write path is closed |
| M2 | Reviewed on an enforcing CNI, **not applied** | Residual in-namespace lateral reach. No non-admin can create pods in `ewallet-prompts`, so M2 is defence in depth: recommended before or together with r33, not a blocker |
| Registry writes | Still unauthenticated (0.4.0) for cluster admins | Detected by M3; availability-only impact |
| Incident F9 | Closed; probe tooling fixed | — |

### 8.2 Exact rollout (requires explicit approval)

The operator runs all steps from a port-forward. **No Jenkins step is involved.**

0. **Freeze.**
   - No other Helm operation on `llm-agent` during the window.
   - Save `helm get values llm-agent -n default -o yaml` as `r32-values.yaml` (private evidence).
   - The chart source is this branch at the approved commit (`infra/helm/llm-agent-platform`, with the `promptSource` template).
1. **Pre-checks**, each must pass:
   - The G3 regression procedure of `m1-apply/g3-regression-post-m1/` is 9/9.
   - The drift identity probe returns `drift-specialist/v2`.
   - Record the config-hash and pod names.
   - `prompt_registry gate --phase post --registry http://127.0.0.1:12121` is PASS.
2. **Managed ConfigMap.**
   - Run `python -m ops.llm_agent.prompt_registry configmap --agent drift-specialist --version v2 --name drift-specialist-prompt-managed --namespace ewallet-agents --registry http://127.0.0.1:12121 > managed.yaml`. The M3 gate runs inside and fails closed.
   - Check the annotations: `prompt-sha256=0c0798e185cd5f86734069b7688badd0ab0aea2a416cab29f62767d8e4375fef` and `prompt-source=registry`.
   - `kubectl apply --server-side --field-manager=ewallet-prompt-operator -f managed.yaml`
3. **Diff gate.**
   - `helm template llm-agent infra/helm/llm-agent-platform -n default -f r32-values.yaml --set agents.specialists.drift-specialist.promptSource=registry` against the live manifest. The semantic diff must be **exactly** the 2 fields of §3. Otherwise stop.
4. **Upgrade.**
   - `helm upgrade llm-agent infra/helm/llm-agent-platform -n default -f r32-values.yaml --set agents.specialists.drift-specialist.promptSource=registry --wait` → r33.
5. **Post-checks:**
   - Agent `drift-specialist` Ready.
   - config-hash and pod unchanged (the content is identical, as in the kind canary).
   - Drift probe `drift-specialist/v2`.
   - G3 regression 9/9.
   - `helm get manifest` r32 vs r33 differs only in the 2 fields.
   - gate `post` PASS.
6. **Rollback** on any failure after step 4:
   - `helm rollback llm-agent 32 -n default --wait`, then re-run the post-checks against r32.
   - The managed ConfigMap is not chart-owned. It may stay; it is inert without r33.

Abort before step 4 leaves production untouched. The managed ConfigMap alone has no consumer.
