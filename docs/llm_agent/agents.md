# kagent Coordinator, Specialists and registry

Status on 2026-11-02: **G3 runtime previously verified; current readback
blocked; P1/P2 source ready; P3/P4 approval gated.** kagent remains pinned to
`0.10.3`. Never infer current cluster state from historical evidence.

## Registry and capabilities

Kubernetes `Agent` and `RemoteMCPServer` custom resources are the source of
truth. No second registry service is introduced. The chart publishes an
explicit version, description and `a2a` skill on every Agent Card, while
`scripts/llm_agent/agent_platform_probe.py` validates a captured inventory,
generated workload ownership, readiness, card capabilities and MCP acceptance
without reading or emitting Secret values.

| Resource | Role | Exposed capability | MCP grants |
|---|---|---|---|
| `fraud-coordinator` | Coordinator | `investigate-transaction` | none; delegates by A2A |
| `drift-specialist` | Drift/risk specialist | `feature-risk-analysis` | `feature_lookup`, `drift_detection` |
| `feature-rag-specialist` | Fraud evidence specialist | `feature-rag-evidence` | `feature_lookup`, `fraud_scoring`, `knowledge_search` |

Two `RemoteMCPServer` resources (`feature-rag-mcp`, `drift-mcp`) point only to
the MCP Gateway. The Coordinator has no business MCP tool. Tool grants remain
enforced at the MCP server by credential subject and do not come from Agent
Card metadata.

Committed G3 evidence records all three Agents as `Accepted=True` and
`Ready=True`, both RemoteMCPServers as `Accepted=True`, and a later runtime
summary records Helm `llm-agent` revision 24, `llm-agent-mcp` revision 3 and
`kagent` revision 2. Those files are historical evidence. A fresh 2026-11-02
readback was impossible because the active public IP is outside the GKE master
authorized network; therefore current runtime status is `BLOCKED_READBACK`, not
re-verified.

## Multi-replica ownership

For `agents.mode=agent`, native `autoscaling/v2` HPA targets each exact kagent
generated Deployment. When enabled, the chart omits Agent deployment
`replicas`; kagent 0.10.3 then owns the pod template while HPA owns replica
count. The G3 source profile is bounded to 2–3 replicas, CPU target 60%, one-pod
scale steps, a five-minute scale-down stabilization window and one PDB per
Agent. The chart renders no KEDA object in this mode.

This is **source readiness only**. Runtime PASS still requires Metrics Server,
node headroom, two ready replicas, measured request distribution, scale-out,
scale-in, controller-backed task readback, no duplicate execution, auth
negative tests and the existing G3 business regression. No cloud rollout ran
in this campaign.

BYO runtimes use controller-backed `KAgentTaskStore` when `KAGENT_URL` is set;
the in-memory store is local-only. This is the intended cross-replica task
state contract, but it must be verified under real distribution before the
rubric row is promoted.

## A2A, prompts and fail-closed behavior

The business path remains `Coordinator -> A2A Gateway -> Specialist -> MCP
Gateway`. Exact Specialist routes and mesh policies remain unchanged. The
gateway does not expose a dynamic host forwarding fallback. Prompt files are
versioned under `infra/helm/llm-agent-platform/prompts`; their manifest hashes
must match at render time. Tool and model failures remain explicit rather than
being converted into fabricated business results.

## SandboxAgent and Agent Substrate

The production G3 path is not silently converted to SandboxAgent. The isolated
pilot in `infra/kubernetes/kagent-pilot` pins kagent `0.10.3` and Agent
Substrate `0.0.9`, uses a deterministic internal mock model, has no production
tools or credentials, restricts network access to the mock service and applies
resource/security bounds.

Its reproducible chart render validates 80 documents and correctly returns
`BLOCKED_PENDING_OWNER_APPROVAL`: 25 objects are cluster-scoped and the
`atelet` DaemonSet is privileged with a hostPath. Consequently sandbox runtime,
gVisor isolation, snapshot/suspend/resume and Agent Fleet behavior are not
deployed or runtime verified under this directive. See
`infra/kubernetes/kagent-pilot/README.md` for the exact gate and rollback
boundary.

## Reproduction

```bash
python3 -m unittest \
  tests.llm_agent.test_agent_platform_probe \
  tests.llm_agent.test_agents_chart \
  tests.unit.test_kagent_bounded_pilot

render_dir="$(mktemp -d /tmp/kagent-pilot-render.XXXXXX)"
infra/kubernetes/kagent-pilot/scripts/render.sh "${render_dir}"
cat "${render_dir}/approval-gate.json"
```

Use `docs/llm_agent/agent_platform_20261102.md` for the complete gap matrix,
runtime acceptance gates and evidence classification.
