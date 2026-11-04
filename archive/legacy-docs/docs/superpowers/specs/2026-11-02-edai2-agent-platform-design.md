# EDAI-2 Agent Platform design

Date: 2026-11-02

## Scope and fixed baseline

Extend the deployed kagent 0.10.3 platform without changing the frozen G3
business path, MCP grants, feature contracts, model, RAG corpus, Secrets,
Gateways, or observability topology. The implementation remains on
`codex/llm-g2-llmd-routing`; canonical and main are out of scope.

The active business path remains:

`Client -> Istio Gateway -> BYO Coordinator -> A2A Gateway -> BYO Specialists
-> MCP Gateway -> Feast/KServe/RAG`.

The authoritative rubric targets are EDAI-2 rows 8, 16, 19-21, 25-27, 40 and
41. Rows 19 and 25 require multi-replica autoscaling. Rows 20 and 27 require a
sandbox and Agent Fleet/Substrate evidence.

## Current-state findings

- Three kagent `Agent` resources and two `RemoteMCPServer` resources were
  runtime-verified during G3. Agent Cards, controller task persistence, A2A
  delegation and MCP authorization already exist.
- The production G3 overlay deliberately sets one replica and does not enable
  autoscaling.
- kagent 0.10.3 `Agent` and `SandboxAgent` CRDs do not expose a scale
  subresource. Their generated Deployments can be targeted by HPA, provided
  the Agent deployment `replicas` field is omitted. The pinned controller
  intentionally preserves an HPA-managed replica count when desired replicas
  are nil.
- BYO runtimes use controller-backed `KAgentTaskStore` in cluster. The
  `InMemoryTaskStore` path is local-only, so task reads can survive request
  distribution and worker restarts when `KAGENT_URL` is configured.
- kagent 0.10.3 `SandboxAgent` is implemented by Agent Substrate 0.0.9. It is
  not a standalone Pod sandbox. Installing it requires new CRDs, controllers,
  data-plane services and a privileged/host-integrated atelet DaemonSet, which
  is outside the current mutation approval.
- GKE control-plane readback is currently blocked because the active public
  IPv4 is not in the master authorized network. Changing that network is not
  authorized by this campaign.

## Selected design

### P1: registry and lifecycle

Keep Kubernetes `Agent` CRDs as the registry source of truth. Add a read-only,
fail-closed probe that inventories Agent, RemoteMCPServer, generated
Deployment/Service, Agent Card, readiness and ownership. It emits a sanitized
JSON record suitable for checksumming and evidence. No standalone registry is
introduced.

### P2: multi-replica and autoscaling

Use native `autoscaling/v2` HPA for `agents.mode=agent` and target each
controller-owned Deployment by exact name. When HPA is enabled, omit
`spec.*.deployment.replicas` from every Agent so the kagent reconciler preserves
the HPA-owned value. Use CPU utilization with a warm floor of two and a hard
maximum of three in the GKE Agent Platform profile. Add bounded scale-up/down
behavior and a PodDisruptionBudget for each Agent.

Keep the existing KEDA `ScaledObject` design only for
`agents.mode=sandbox`, where the supported scale target is a Substrate
`WorkerPool`. Do not install KEDA solely for the current Agent/BYO path.

This design does not claim runtime PASS until the cluster shows two healthy
replicas, load distribution, scale-out, scale-in, controller-backed task
readback, security-negative tests and G3 regression PASS.

### P3/P4: sandbox and Substrate

Keep the isolated pilot separate from production G3. It uses a deterministic
mock model, no production tools, no production Secrets, a restricted domain
allow-list, resource limits and no external Service. Validate every manifest
against the pinned kagent 0.10.3 and Substrate 0.0.9 schemas and chart digests.

Do not install Substrate in GKE under this directive. Record the exact
cluster-scoped/privileged resources, resource requests, storage, rollback and
approval gate. Runtime claims for gVisor, Actor suspend/snapshot/resume and
fleet behavior remain BLOCKED until the owner separately approves that
infrastructure.

## Safety and rollback

- Source changes are additive and default-disabled.
- The GKE profile changes only Agent replica ownership, HPAs and PDBs when the
  owner later performs an approved Helm upgrade.
- Rollback is a Helm rollback to the preceding `llm-agent` revision. Disabling
  autoscaling restores explicit Agent replicas on the next render.
- No new nodes, IAM, RBAC, Secrets, models, data or cluster-scoped resources
  are created in this task.

