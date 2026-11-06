# Agent platform completion (Batch C-B)

This page records what Batch C-B built for CI/CD, KEDA, MCP failover and Sandbox, and what remains unproven. All of it is local. **No rubric row changes status from this work**, because none of it has run on the cluster.

To run it, use the [runbook](../../runbooks/edai2/llm_agent/C_AGENT_PLATFORM_RUNBOOK.md).

## Jenkins CD (rows 34–36)

`scripts/ci/bounded_cd.py` adds one release flow for MCP servers, agents and streaming. It applies the release to `ewallet-cicd`, reads the digest back, runs a smoke Job and parks the workload at zero replicas. A failed run restores the previous state.

The CD stops at `ewallet-cicd` by design. Jenkins has no rights in production namespaces, and widening them is a separate decision. Rows 34–36 ask for CI/CD of these components, and this verifies a release candidate; it does not roll one out to production.

Each `release/<component>` branch now ships only that component. Without this, the new deployable components would have been included in a RAG release.

## KEDA (rows 19, 22, 25)

The chart has `agents.autoscaling.engine`, with `hpa` as the default. With `keda`, a ScaledObject replaces the HPA, so one Deployment never has two autoscalers. Scaling follows `ewallet_agent_inflight_tasks`, because agent work waits on the LLM and uses little CPU.

KEDA needs one new network flow to Prometheus. Only the operator pod joins the mesh, which gives it the identity the rule admits.

## MCP failover (row 18)

agentgateway v1.7.0 does not retry MCP backends, so failover is in the client. `with_failover` repeats a call only when every tool in it is read-only and the failure was in transport, within one overall deadline. A future tool with side effects stays outside the read-only list and is never retried.

Two spread replicas remove the single point of failure. `mcp-gateway` also goes to two replicas, since it was a single point of failure too.

## Sandbox (rows 16, 20, 22)

`infra/kubernetes/agent-sandbox/` holds the business agents as `SandboxAgent` objects. They pass the pinned CRD schema, but **no BYO agent has booted in a sandbox yet**, so the work is split into S0 boot, S1 tools and S2 A2A.

## Rubric position

| Row | Implemented | Local validated | Cloud verified | Evidence complete |
|---|---|---|---|---|
| 18 MCP fallback | Yes | Yes | No | No |
| 19 Agent KEDA | Yes | Yes | No | No |
| 20 Agent in Sandbox | Manifests only | Schema only | No (pilot only) | No |
| 22 Second agent | Partly | Yes | Partly | No |
| 25 Coordinator autoscale | Yes | Yes | No | No |
| 34 CI/CD MCP and data agent | Yes | Yes | No | No |
| 35 CI/CD Coordinator | Yes | Yes | No | No |
| 36 CI/CD streaming | Yes | Yes | No | No |
| 6 Benchmark and optimize | Rerun prepared | Yes | Historical only | Historical only |
