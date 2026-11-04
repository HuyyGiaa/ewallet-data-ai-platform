# EDAI-2 Agent Platform closure report — 2026-11-02

## Scope and immutable baseline

This campaign extends the existing G3 source on
`codex/llm-g2-llmd-routing`. It does not change business tools, models, Feast,
KServe, RAG data, MCP credentials, Gateways, IAM, cluster RBAC or unrelated
Helm releases. Canonical/main and all other worktrees remain untouched.

The authoritative rubric scope is EDAI-2 rows 8, 16, 19–21, 25–27, 40 and 41.
Historical G3 runtime evidence is preserved, but no historical snapshot is
reported as a current readback.

## Gap and verdict matrix

| Priority | Requirement | Source verdict | Current runtime verdict | Evidence / remaining gate |
|---|---|---|---|---|
| P1 | Agent registry, lifecycle, cards, capabilities, MCP registration, UI/API health | PASS | HISTORICAL_VERIFIED; CURRENT_READBACK_BLOCKED | Three Agents/two RemoteMCPServers exist in G3 evidence; fail-closed captured-inventory probe added. Fresh GKE API blocked by master authorized network. |
| P2 | Coordinator and specialists multi-replica/autoscaling | PASS | BLOCKED | Three native HPAs and PDBs render with 2–3 bounds. Needs approved rollout, load, scale-out/in, state, duplicate-execution, auth and G3 regression evidence. |
| P3 | Isolated agent sandbox | PASS_SOURCE | BLOCKED_PENDING_OWNER_APPROVAL | Deterministic no-tool pilot and schema/digest validation pass. Substrate installation is cluster-scoped and privileged. |
| P4 | Agent Substrate / fleet / warm-up | PASS_FEASIBILITY | BLOCKED_PENDING_OWNER_APPROVAL | kagent 0.10.3 + Substrate 0.0.9 pinned. Render: 80 documents, 25 cluster-scoped, one privileged hostPath DaemonSet. No install performed. |

## P1 registry contract

The registry contract is versioned at
`infra/kubernetes/kagent-platform/agent-registry-contract.json`. The probe in
`scripts/llm_agent/agent_platform_probe.py` reads only supplied JSON captures,
requires complete expected inventory, verifies ownership/readiness, compares
Agent Card version/skills/capabilities and checks RemoteMCPServer acceptance.
It redacts token-, authorization-, cookie- and Secret-like fields before
writing its report and fails closed on missing or inconsistent evidence.

Runtime reproduction requires fresh, redacted captures of Agents,
RemoteMCPServers, Deployments, Services and all three Agent Cards. That capture
was not possible in this run because GKE rejected the client network before
authentication.

## P2 supported scaling design and runtime gate

`agents.mode=agent` uses HPA on the generated Deployments. Agent CR replica
fields are absent while autoscaling is enabled, preventing reconciliation from
overwriting HPA. Each Agent gets a PDB. G3 renders exactly three HPA resources,
three Agents, no ScaledObject and retains the existing routes, policies, MCP
resources and immutable image/provenance validation.

Runtime acceptance is atomic: two ready replicas, balanced requests, bounded
scale-out to at most three, scale-in to two after stabilization, task reads
from a different replica, no duplicate tool execution, all auth-negative tests
and the current G3 business path must pass. Otherwise rollback is the prior
`llm-agent` Helm revision. No node creation is allowed.

## P3/P4 approval boundary

The pinned render contains 12 CRDs; five ClusterRoles; five
ClusterRoleBindings; one cluster-scoped SandboxConfig; one
ValidatingAdmissionPolicy and one binding. `DaemonSet/atelet` is privileged and
mounts hostPath `run-ateom`. These facts require a separate owner decision;
they are not a technical inconvenience that can be bypassed with broader
permissions.

The future isolated pilot must prove deterministic Agent response, denied
external network, denied production Secret/API access, enforced resources and
snapshot/suspend/resume. It must collect ownership before any cleanup and must
not automatically delete CRDs, PVs or shared cluster-scoped objects.

## Exact blockers

1. GKE public endpoint readback timed out. Cluster configuration allows only
   `113.188.119.38/32`, while the active public IPv4 observed during this audit
   was `171.239.81.235`. Modifying master authorized networks was not approved.
2. Installing Substrate would add the cluster-scoped and privileged objects
   listed above. This campaign explicitly requires a separate approval.
3. P2 cannot be promoted from source-ready to runtime-verified without an
   approved Helm mutation and a measured scale campaign.

These blockers are independent: source tests and reproducible renders continue
to completion, but runtime claims remain blocked.
