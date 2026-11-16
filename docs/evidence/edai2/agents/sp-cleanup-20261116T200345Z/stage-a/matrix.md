# Legacy Specialist Deployment cleanup — KEEP / REMOVE / MODIFY matrix (campaign sp-cleanup-20261116T200345Z)

Owner of every removed object: Helm release `llm-agent` (namespace `default`) rendering `agents.specialists.*`,
then the kagent controller (Agent CR -> Deployment/Service/ConfigMap/Secret via ownerReferences).
Mechanism: `agents.specialists.<name>.runtime: sandbox` (overlay `agi/specialists-sandbox-only.yaml`, package `sp-cleanup`).
Rollback: `helm rollback llm-agent <rev-before>` (token SP-CLEANUP-ROLLBACK); kagent recreates the Deployments.

## REMOVE (Helm, 42 objects; generated children removed by kagent garbage collection)
| Kind | Objects | Why |
|---|---|---|
| Agent (BYO) | drift-specialist, feature-rag-specialist | Legacy Deployment runtime; Coordinator delegates only via sandbox routes |
| (kagent-generated) Deployment, Service, config Secret/ConfigMap | same names | Removed by kagent with the Agent CR (not deleted by hand) |
| ScaledObject (+ KEDA HPA) | drift-specialist, feature-rag-specialist | Scaled the removed Deployments |
| PodDisruptionBudget | drift-specialist, feature-rag-specialist | Guarded the removed pods |
| ServiceAccount | drift-specialist, feature-rag-specialist | Identity of the removed pods only |
| ConfigMap | drift-specialist-prompt-v2, feature-rag-specialist-prompt-v5 | Prompt mounts of the removed pods (prompts stay in chart + registry) |
| AuthorizationPolicy | 10 (`*-to/from-<specialist>*`) | Mesh paths of the removed pods |
| NetworkPolicy | 22 (`*-<specialist>-*`) | Network paths of the removed pods |

## MODIFY (6 objects)
| Object | Change |
|---|---|
| ConfigMap a2a-gateway-config | the two `x-kagent-host` Deployment routes dropped; `sandbox-*` routes byte-identical |
| Deployment a2a-gateway | config checksum only (rolls the gateway) |
| Agent fraud-coordinator | annotation `ewallet.io/a2a-gateway-config-sha256` only (rolls the Coordinator); image, env, sandbox specialists unchanged |
| ConfigMap llm-agent-network-flows | flows of the removed pods dropped |
| NetworkPolicy allow-byo-modelconfig-to-kube-apiserver | removed SA dropped from the pod selector |
| RoleBinding ewallet-global-llm-reader | removed SA subject dropped |
| Registry metadata (registry_catalog.py, code only) | runtime text "SandboxAgent only, no BYO Deployment"; no `byo-image-digest` label; entries, versions, prompts kept. Republishing the registry is a separate approval |
| AGI-08 A2A negative control | control is the sandbox route `/api/a2a-sandboxes/ewallet-sandbox/drift-specialist/` (was the Deployment route) |

## KEEP (must be unchanged; unexpected change = blocker)
Coordinator (image/env), SandboxAgents + fleet (ewallet-sandbox), WorkerPool business-agents, ScaledObject
business-agents-workerpool, coordinator ScaledObject, Substrate/atelet/ateapi, kagent, A2A Gateway sandbox routes,
MCP Gateway (ambient-backend-token), MCP servers (ewallet-mcp/*, ewallet-agents/drift-mcp), backends, OTel/Prometheus/Grafana,
registry entries for all three agents, chart values `agents.specialists.*` (tools, prompts, byo image used by release_state.py).

## KEEP with residual risk (no action without separate approval)
| Item | Note |
|---|---|
| Secrets mcp-token-{feature-rag,drift}-specialist-* (not Helm-managed) | No workload mounts them after cleanup; still valid MCP backend credentials used by AGI-08 negatives. Revocation/rotation = separate owner approval |
| cloud_lab profile `minimal.one-agent` | lists ewallet-agents/drift-specialist; never run; missing workloads are skipped |
| dashboards/logs-operations.json Loki regex | names the specialists; matches nothing after cleanup (panel runtime-pending) |
| Jenkins RBAC proposals / components.json / build scripts | proposals and image builds for the BYO image the SandboxAgents still use |
