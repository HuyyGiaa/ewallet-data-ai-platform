# Policy-only MCP Gateway and A2A gateway

Status:
- **SOURCE_READY + TEST_PASS.**
- **LOCAL_POLICY_SMOKE (Docker, deny path).**
- **Identity: DEPLOY_NO_GO.**

Both gateways are disabled by default (`mcpGateway.enabled`,
`a2aGateway.enabled`). Rubric edai-2 row 23 asks for agentgateway as the MCP
Gateway; row 24 asks for agentgateway as the A2A Gateway.

## Roles (four distinct gateways)

| Gateway | Workload | Purpose |
|---|---|---|
| External | Istio Gateway API (`gateway.yaml`) | Analyst → Coordinator A2A |
| LLM | `agentgateway` | ModelConfig → (llm-d) → vLLM model policy |
| MCP | `mcp-gateway` (agentgateway v1.7.0, standalone) | Specialists → MCP servers, policy only |
| A2A | `a2a-gateway` (same image) | Native kagent A2A (`A2A_BASE_URL`) → kagent controller |

The MCP Gateway hosts no tools and no agent logic. The A2A gateway proxies the
native kagent A2A protocol with the agentgateway `a2a` policy, which rewrites
agent cards to point at the gateway. It is not a separate A2A runtime.

## MCP Gateway policy (per route `/feature-rag/mcp`, `/drift/mcp`)

- One MCP target per route. Tool names are not prefixed
  (`prefixMode: conditional`), so the RemoteMCPServer `toolNames` stay valid.
- `mcpAuthorization`, one rule per specialist:
  `has(source.identity) && namespace == ewallet-agents && serviceAccount ==
  <read-back worker SA> && mcp.tool.name in <that specialist's allow-list>`.
  The allow-list comes from `agents.specialists.*.tools`, the same source as
  the SandboxAgent `toolNames`. A route with no read-back identity renders
  `'false'`. Denied tools are filtered from `tools/list`; a denied
  `tools/call` returns JSON-RPC -32602 "unknown resource" without reaching
  the server.
- `requestHeaderModifier.remove`: `authorization`, `x-ewallet-actor` and
  `x-ewallet-caller`. Then `transformations.request.set x-ewallet-caller =
  source.identity.serviceAccount`.
- Request rate limit (20/s per replica), 30 s request timeout, JSON logs.
- Bypass: MCP servers admit only `sa/mcp-gateway`, through an Istio
  AuthorizationPolicy plus a NetworkPolicy pair
  (`mcp-gateway-to-{feature-rag,drift}-mcp`). No agent sandbox lists an MCP
  server domain.

## Verification done (2026-10-31, no cluster)

1. `scripts/llm_agent/validate_gateway_configs.sh`: the pinned agentgateway
   v1.7.0 binary (`--validate-only`, no network) accepts all six configs the
   chart renders: LLM, LLM+llm-d, MCP deny-all, MCP with callers, A2A
   deny-all and A2A with caller. It rejects a negative control (unknown field).
   Invalid CEL was shown to be rejected too. Evidence:
   `docs/evidence/llm_agent/source_review/gateways/agentgateway_validate_only.txt`.
2. `scripts/llm_agent/gateway_smoke/run.sh` runs the chart-rendered MCP
   Gateway in front of a header-recording MCP stub on an internal Docker
   network. Evidence: `mcp_gateway_policy_smoke.json`.
   - **deny-all** and **callers**: no Istio identity exists in Docker, so
     `tools/list` returns `[]`, both `tools/call` return 400/-32602, and the
     stub received **no** `tools/call`.
   - The client sent forged `x-ewallet-actor`, `x-ewallet-caller` and
     `Authorization: Bearer forged`. The stub saw none of them, in every
     variant.
   - **Found and fixed:** the first run showed the forged `Authorization`
     header reaching the backend. `authorization` is now stripped on both
     gateways.
   - The test-only variant with an identity-free rule (never production) proves
     the allow path. `tools/list` shows only `feature_lookup`, which executes,
     while `fraud_scoring` stays denied.

## Identity verdict: DEPLOY_NO_GO

The positive path needs a verified caller identity at the gateway, and that is
not proven:

- agentgateway documents `source.identity` as "the (Istio SPIFFE) identity of
  the downstream connection, if available". Under Istio Ambient without a
  waypoint, ztunnel terminates HBONE mTLS and delivers plaintext to the pod.
  Whether agentgateway then sees an identity is UNVERIFIED.
- Substrate 0.0.9 cannot set the worker pod ServiceAccount (`docs/llm_agent/agents.md`),
  so per-specialist identity is not source-definable.
- kagent 0.10.3 `RemoteMCPServer` can only send static headers from a Secret
  or ConfigMap. A rotating per-agent token cannot be delivered.

Consequences:
- Production keeps every caller ServiceAccount empty, so the gateway denies
  every tool.
- The MCP servers keep `DenyAllAuthenticator`. No header-trusting
  authenticator was added.

Runtime gates to lift it (runbook R4):
1. Read back the worker pod ServiceAccounts per WorkerPool.
2. Show `source.identity` populated at the gateway: either agentgateway as an
   ambient waypoint, or the gateway pod enrolled so that it terminates HBONE.
   Use an access-log CEL field.
3. Show that a spoofed `x-ewallet-caller` is overwritten.
4. Run negative cross-agent calls in-cluster.

Only then implement a server-side `TrustedGatewayAuthenticator` that accepts
`x-ewallet-caller` from `sa/mcp-gateway` alone.
