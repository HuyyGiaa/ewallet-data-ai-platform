# Phase D authentication compatibility preflight

Status: **PROPOSED — AWAITING ARCHITECTURE APPROVAL.** No authentication
adapter is implemented; production keeps `DenyAllAuthenticator`. No RBAC,
IAM, token automount or cluster change was made. Date: 2026-10-31.

The canonical Google Doc (Architecture Freeze, Living Decisions up to 21.263)
returned HTTP 401 from this environment and was **not read**. Frozen contracts
used instead: Istio Ambient + Gateway API, kagent 0.10.3 Coordinator and
Specialists with native A2A, RemoteMCPServer, policy-only MCP Gateway separate
from the LLM agentgateway, ModelConfig → agentgateway → CPU vLLM,
OpenTelemetry, least-privilege tool authorization; plus
`configs/llm_agent/release-v1.json` and the chart's architecture contract.

## Evidence

| Fact | Source | Confidence |
|---|---|---|
| `RemoteMCPServer` v1alpha2 spec: `url`, `protocol`, `headersFrom[{name, value, valueFrom{type,name,key}}]`, `timeout`, `sseReadTimeout`, `terminateOnClose`, `tls`, `allowedNamespaces`. No file-based or refreshed credential source | kagent-crds 0.10.3 chart, digest `sha256:76582530…` (`kagent.dev_remotemcpservers.yaml`), read locally | Verified (schema) |
| `headersFrom` values come from Secrets/ConfigMaps in the Agent's namespace and are sent agent → tool | https://kagent.dev/docs/kagent/0.x/concepts/tools/ (unversioned 0.x docs) | Documented, not pinned to 0.10.3 |
| `SandboxAgent.spec.declarative.deployment` has `serviceAccountName`, `serviceAccountConfig{annotations,labels}`, `volumes` (incl. `projected.serviceAccountToken`), `volumeMounts`, `extraContainers`; no `automountServiceAccountToken` field | kagent-crds 0.10.3 (`kagent.dev_sandboxagents.yaml`) | Verified (schema) |
| The pilot runs SandboxAgents in a Substrate WorkerPool whose pod ServiceAccount is `kagent-pilot-worker` (gVisor) | `infra/kubernetes/kagent-pilot/values/kagent.yaml` | Verified (repo) |
| Substrate already verifies Kubernetes SA JWTs against the GKE cluster issuer (`auth.mode: jwt`, audience `api.ate-system.svc`) | `infra/kubernetes/kagent-pilot/values/substrate.yaml` | Verified (repo) |
| agentgateway: JWT authentication (issuer, audiences, remote JWKS); token removed before the backend unless `preserveToken`; MCP authorization CEL on `mcp.tool.name` and claims (Deny → Require → Allow; 401/403) | agentgateway.dev docs, `latest` (1.7.x): security/jwt/setup, mcp/tool-access, security/authorization | Documented, not pinned to 1.7.0 |
| agentgateway CEL exposes `source.identity{trustDomain,namespace,serviceAccount}`, `source.spiffeId`, available to MCP authorization | agentgateway.dev reference/cel (cel-context-interactive, variables) | Documented; **population under Istio Ambient UNVERIFIED** |
| agentgateway can set/remove request headers (HTTPRoute RequestHeaderModifier; policy `transformation.request.set` with CEL values) | agentgateway.dev traffic-management/header-control, security/jwt/setup | Documented; overwrite of a client-supplied header UNVERIFIED |
| Istio Ambient (no waypoint) carries peer identity in HBONE, not in application headers; no documented header exposing the peer SPIFFE ID to the app | istio.io ambient data-plane and HBONE pages (summarized) | **UNVERIFIED** (not stated verbatim) |
| No approved OIDC/JWT issuer for agents exists in the repository (kagent `auth.mode: unsecure`, oauth2-proxy disabled) | repository search | Verified (repo) |

## The nine questions

| Question | A. Istio identity + trusted MCP Gateway boundary | C. Audience-bound SA token | B. Existing approved JWT/OIDC |
|---|---|---|---|
| 1. Specialist credential | Mesh X.509 SVID of its pod ServiceAccount, issued and held by ztunnel; nothing in the pod | Projected `serviceAccountToken` volume, audience `mcp-gateway` (schema allows it) | None exists |
| 2. Credential reaches the gateway | HBONE mTLS, transparent; RemoteMCPServer only needs the gateway URL | **Not supported:** RemoteMCPServer sends only Secret/ConfigMap/static headers; a file token needs a sidecar (`extraContainers`) or copying tokens into Secrets (breaks rotation, needs write RBAC) | — |
| 3. Gateway verifies the caller | Mesh peer identity in agentgateway CEL `source.identity`/`source.spiffeId` (UNVERIFIED under ambient) | agentgateway JWT authentication against the cluster issuer JWKS | — |
| 4. Original identity across forwarding | Lost at transport level (server sees `sa/mcp-gateway`); the gateway re-asserts it in a header it sets from `source.identity` after removing any client copy | Lost unless re-asserted the same way (agentgateway strips the token by default; passthrough is not wanted) | — |
| 5. Server verifies its caller | Istio AuthorizationPolicy ALLOW only `cluster.local/ns/ewallet-agents/sa/mcp-gateway` on 8080, plus the NetworkPolicy pair; in-process only the asserted subject's format can be checked | Same as A for the gateway hop | — |
| 6. Tool permissions without trusting user headers | Gateway MCP authorization on `source.identity.serviceAccount` × `mcp.tool.name` (primary); server `ToolGrants` on the gateway-asserted subject (defense in depth); client `x-ewallet-actor` removed at the gateway and rejected on mismatch by the server | Same, keyed on JWT `sub` | — |
| 7. Rotation | Automatic SVID rotation by Istio | Kubelet rotates the projected token, but the delivery path in 2 does not follow it | — |
| 8. Extra Kubernetes permissions | None; no token automount, no TokenReview | None for JWKS verification of a public GKE issuer; TokenReview avoided | — |
| 9. New infrastructure | None beyond the approved MCP Gateway workload and ambient enrollment | Sidecar or token-copy controller | An IdP (out of scope) |

## Recommendation (PROPOSED — AWAITING ARCHITECTURE APPROVAL)

**Option A: Istio Ambient workload identity, with the policy-only MCP Gateway
as the single authorization point and a mesh-enforced trusted hop to the MCP
servers.** It is the only option that works with the pinned kagent 0.10.3
without new infrastructure, tokens or RBAC. C fails at credential delivery; B
has no issuer.

Trust boundaries:

1. Specialist pod → MCP Gateway: ztunnel mTLS; identity = Specialist
   ServiceAccount SVID.
2. MCP Gateway (an agentgateway instance distinct from the LLM agentgateway):
   authorizes `source.identity` × `mcp.tool.name`, removes client
   `x-ewallet-actor`, sets it from `source.identity.serviceAccount`.
3. MCP Gateway → MCP server: ztunnel mTLS; AuthorizationPolicy admits only
   `sa/mcp-gateway`; NetworkPolicy ingress only from gateway pods.
4. MCP server: a future `TrustedGatewayAuthenticator` maps the gateway-set
   header to a `Principal(mechanism="mesh-gateway-assertion")`, then
   `ToolGrants` applies. Its `readiness()` must report
   `authentication_not_configured` unless explicitly enabled.

Prerequisites, each a runtime gate before approval becomes VERIFIED:

- One Substrate WorkerPool per Specialist, each with its own ServiceAccount
  (`feature-rag-specialist`, `drift-specialist`). With the shared
  `kagent-pilot-worker` pool, Specialists are indistinguishable in the mesh.
- `ewallet-agents` (and the WorkerPool namespace) enrolled in ambient, with
  egress from the gVisor sandbox confirmed to leave through the captured pod
  network namespace.
- Runtime proof that agentgateway populates `source.identity` under ambient,
  and that its header `set` overwrites a client-supplied value.
- Positive and negative connectivity tests: Specialist → gateway allowed; any
  other pod → MCP server denied; forged `x-ewallet-actor` through the gateway
  overwritten.

Limitations:

- The MCP server cannot verify the asserted identity cryptographically in
  process. If ambient enrollment or the AuthorizationPolicy is missing, the
  header becomes forgeable by anything that can reach port 8080. Default-deny
  NetworkPolicy is the second layer.
- The local kind cluster currently has Istio installed but the namespace not
  enrolled, and ztunnel is fragile there (host inotify limit). No part of this
  design is runtime-verified.
- If `source.identity` proves unavailable at the gateway, the fallback is C
  with a decision on token delivery (sidecar or a kagent change).

## Proposed Living Decision (for review; not applied to Google Docs)

> **21.264 (proposed) — MCP caller authentication.** MCP tool calls are
> authenticated by Istio Ambient workload identity. The policy-only MCP Gateway
> (agentgateway, separate from the LLM agentgateway) authorizes each call on
> the mesh source identity and the MCP tool name, removes client-supplied
> identity headers and asserts the verified Specialist ServiceAccount to the
> MCP servers. The MCP servers admit only the MCP Gateway principal (Istio
> AuthorizationPolicy plus NetworkPolicy) and re-check per-tool grants on the
> asserted subject. Each Specialist runs in its own Substrate WorkerPool with a
> dedicated ServiceAccount. No ServiceAccount token automount, TokenReview or
> new identity provider. Until runtime gates (ambient enrollment,
> `source.identity` population, header overwrite, negative connectivity) pass,
> the MCP servers keep `DenyAllAuthenticator`.
