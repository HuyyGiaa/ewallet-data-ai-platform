# Agent platform completion (Batch C-B)

This page records what Batch C-B built for CI/CD, KEDA, MCP failover and Sandbox, and what remains unproven. All of it is local. **No rubric row changes status from this work**, because none of it has run on the cluster.

To run it, use the [runbook](../../runbooks/edai2/llm_agent/C_AGENT_PLATFORM_RUNBOOK.md).

## Jenkins CD (rows 34–36)

`scripts/ci/bounded_cd.py` adds one release flow for MCP servers, agents and streaming. It applies the release to `ewallet-cicd`, reads the digest back, runs a smoke Job and parks the workload at zero replicas. A failed run restores the previous state.

The CD stops at `ewallet-cicd` by design, because Jenkins has no rights in production namespaces. The [least-privilege design](../cicd/PRODUCTION_ROLLOUT_RBAC.md) is proposed, not applied. Rows 34–36 ask for CI/CD of these components, and this verifies a release candidate; it doesn't roll one out to production.

The streaming release has no source dependency on C-A, but it should still ship from the merged commit, so it carries C-A's watermark fix. See [the integration order](../platform/C_INTEGRATION_ORDER.md).

Each `release/<component>` branch now ships only that component. Without this, the new deployable components would have been included in a RAG release.

## KEDA (rows 19, 22, 25)

The chart has `agents.autoscaling.engine`, with `hpa` as the default. With `keda`, a ScaledObject replaces the HPA, so one Deployment never has two autoscalers. Scaling follows `ewallet_agent_inflight_tasks`, because agent work waits on the LLM and uses little CPU.

KEDA needs one new network flow to Prometheus. Only the operator pod joins the mesh, which gives it the identity the rule admits.

The pinned chart and our rendered ScaledObject were tested locally against a stub Prometheus: scale-out, the cap, outage fallback, recovery and rollback all worked. KEDA's webhook refuses a ScaledObject while the HPA exists, so the runbook deletes the HPA first. The real gauge and mesh path remain unproven.

## MCP failover (row 18)

agentgateway v1.7.0 does not retry MCP backends, so failover is in the client. `with_failover` repeats a call only when every tool in it is read-only and the failure could heal on another replica: a lost connection, or a 502, 503 or 504. A rejected credential, a tool error and an unknown exception are not retried, within one overall deadline. A test fails until each new tool is classified, so an unaudited tool can't become retryable.

Two spread replicas remove the single point of failure, and `mcp-gateway` also goes to two replicas. The experiment runs in its own namespace, so no production pod is deleted.

## Sandbox (rows 16, 20, 22)

`infra/kubernetes/agent-sandbox/` runs the Feature-RAG specialist as a `SandboxAgent` beside an identical BYO `Agent`, in a dedicated namespace and worker pool. The manifests follow rules read from the kagent v0.10.3 source: an explicit command, port 80 and explicit paths. `EXP-C5-SANDBOX` has a driver and a local simulation, and the real agent boots locally with the candidate's command.

**No BYO agent has booted in gVisor yet**, so the work is staged as S0 boot, S1 tools and S2 compare. The README lists four questions only the cluster can answer, starting with whether a non-root process can bind port 80.

## Rubric position

| Row | Implemented | Local validated | Cloud verified | Evidence complete |
|---|---|---|---|---|
| 18 MCP fallback | Yes | Yes | No | No |
| 19 Agent KEDA | Yes | Yes | No | No |
| 20 Agent in Sandbox | Manifests and driver | Simulation and local boot | No (pilot only) | No |
| 22 Second agent | Partly | Yes | Partly | No |
| 25 Coordinator autoscale | Yes | Yes | No | No |
| 34 CI/CD MCP and data agent | Yes | Yes | No | No |
| 35 CI/CD Coordinator | Yes | Yes | No | No |
| 36 CI/CD streaming | Yes | Yes | No | No |
| 6 Benchmark and optimize | Rerun prepared | Yes | Historical only | Historical only |
