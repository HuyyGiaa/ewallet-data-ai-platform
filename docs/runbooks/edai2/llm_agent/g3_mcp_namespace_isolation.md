# G3 R1A MCP namespace isolation

Status: **SOURCE/STATIC VERIFIED ONLY**. Nothing in this document proves a live
GKE rollout, gateway cutover or business investigation.

## Architecture and ownership

Before R1A, the `llm-agent` release owns the MCP Gateway and legacy MCP server
workloads in `ewallet-agents`. kagent's namespaced Roles let its controller read
Secrets and create workloads there.

R1A adds a second, disjoint release:

| Owner | Namespace | Resources during blue/green |
|---|---|---|
| `kagent` | `kagent`, `ewallet-agents` | unchanged controller RBAC and CRDs |
| `llm-agent` | `ewallet-agents` | Agents, ModelConfig, RemoteMCPServers, the single MCP Gateway, and legacy MCP servers retained for rollback |
| `llm-agent-mcp` | `ewallet-mcp` | two MCP Deployments/Services/ServiceAccounts, namespace security policies and scoped MLOps policies |
| platform operator | cluster scope | `Namespace/ewallet-mcp`, managed independently from both Helm releases |
| credential operator | both | immutable client Secrets in `ewallet-agents`; immutable server tables in `ewallet-mcp` |
| platform-input operator | `ewallet-mcp` | approved Redis connection Secret copy and immutable registry ConfigMaps |

No resource identity `(kind, namespace, name)` is shared by the two Helm
releases. `ewallet-mcp` is not added to kagent `rbac.namespaces` or
`watchNamespaces`.

`Namespace/ewallet-mcp` is defined by
`infra/kubernetes/llm-agent-mcp/namespace.yaml`, not by the
`llm-agent-mcp` release. The application profile sets `namespace.create=false`.
The platform operator must apply and verify the namespace before Helm install.
Consequently, uninstalling `llm-agent-mcp` cannot delete the namespace, server
authentication Secrets or platform-input objects, because none is present in
the release manifest. Namespace deletion is never part of application rollback.

The R1A-S2 namespace operation is deliberately separate from Helm:

```bash
kubectl --context "$KUBE_CONTEXT" get namespace ewallet-mcp -o yaml
kubectl --context "$KUBE_CONTEXT" diff \
  -f infra/kubernetes/llm-agent-mcp/namespace.yaml
kubectl --context "$KUBE_CONTEXT" apply \
  -f infra/kubernetes/llm-agent-mcp/namespace.yaml
kubectl --context "$KUBE_CONTEXT" get namespace ewallet-mcp \
  -o jsonpath='{.metadata.labels}'
```

An exact `NotFound` from the first command permits creation; every other error
stops the operation. Before `apply`, an existing namespace must be rejected if it has
`meta.helm.sh/release-name`/`meta.helm.sh/release-namespace` ownership
annotations or conflicting Ambient/Pod Security/lifecycle-owner labels. The
read-back must match the committed manifest exactly for those labels. There is
no namespace delete command in either forward or rollback procedures.

The existing MCP Gateway remains the only MCP entrypoint. Before cutover it
uses local Services. The cutover profile changes only its backend hosts to:

- `drift-mcp.ewallet-mcp.svc.cluster.local:8080`;
- `feature-rag-mcp.ewallet-mcp.svc.cluster.local:8080`.

Agents and RemoteMCPServers continue calling
`mcp-gateway.ewallet-agents.svc.cluster.local`; no direct Agent-to-MCP route is
introduced.

## Static network guarantees

The isolated namespace has Istio and Kubernetes default-deny. The only MCP
server ingress rules select both namespace `ewallet-agents` and the
`mcp-gateway` pod labels; Istio additionally requires the
`cluster.local/ns/ewallet-agents/sa/mcp-gateway` principal. DNS and ambient
kubelet probe paths are explicit.

Only `feature-rag-mcp` receives egress to the reviewed Redis port in
`feature-store` and KServe port in `fraud-serving`. RAG uses its immutable local
registry/model inputs and the same approved Redis connection. `drift-mcp`
receives neither backend flow. NetworkPolicies are additive: R1A-S2 must read
back every policy selecting these workloads and fail if another broad allow
would weaken this model.

Static policy does not prove live enforcement. It also cannot prevent a fully
compromised kagent controller from reading a specialist token and creating a
Pod under a specialist or gateway ServiceAccount inside `ewallet-agents`.
Namespace isolation protects MCP server tables, runtime configuration and MCP
workloads; it does not replace service authentication or contain every action
of a compromised controller.

## Platform inputs

`ops.llm_agent.g3_platform_inputs` requires an explicit destination namespace.
Copying `feast-online-redis` additionally requires
`--approve-sensitive-copy`, checked before Secret material is read. The copy
creates a second credential instance: source rotation does not update it, so
R1A-S2 needs a rotation owner, source resourceVersion/digest evidence (never
payload evidence), and an expiry/cleanup plan. The MLOps Secret remains the
source of truth.

Registry ConfigMaps are immutable and SHA-256 annotated. Their origin and
digest are evidence; their binary payload is not written to Git.

## Source profiles

- `g3/g3.yaml`: legacy/runtime contract; feature-rag PDB disabled at one
  replica, drift PDB retained.
- `g3/mcp-isolated.yaml`: render only isolated MCP workloads and policies for
  the `llm-agent-mcp` release.
- `g3/mcp-cutover.yaml`: point the existing gateway at `ewallet-mcp` while
  preserving legacy MCP Deployments and Services.

Image references and identity are still supplied as immutable digest plus exact
Git SHA. Helm renders references only; it never renders credential payloads.

## R1A-S1 static verification (2026-11-02)

The verified base is commit
`9ff04193fcbfd9d204d469c89a4825af08317458` and tree
`59a69d30c424650f432b4ba903342c848a2f2455`. Checks ran from the locked
`infra/docker/llm-agent-tooling` project; neither `pyproject.toml` nor `uv.lock`
changed.

- `uv lock --check` and `uv sync --locked --all-extras --group dev`: PASS
  (120 resolved, 99 checked).
- Complete `tests/llm_agent` discovery: 440 tests, 427 PASS, 0 FAIL/ERROR,
  13 SKIP. Eight G2 harness cases require the external
  `G2_ROUTER_CHART`; one embedding case requires the pinned model cache; four
  Trivy cases require Docker plus the pinned Trivy image. G2's prior MIXED
  runtime verdict is unchanged.
- `helm lint` for legacy, isolated and cutover value sets: 3/3 PASS.
- Legacy render comparison against the base commit: 95 -> 94 resources; the
  only removal is `PodDisruptionBudget/ewallet-agents/feature-rag-mcp`, with
  zero added or changed common resources.
- Isolated render: 22 resources (two Deployments, Services and
  ServiceAccounts; one drift PDB; three AuthorizationPolicies;
  ten NetworkPolicies; two non-secret ConfigMaps). It renders no Secret,
  Namespace, Role, RoleBinding, cluster-wide RBAC, gateway, Agent,
  RemoteMCPServer or ModelConfig object. Resource-identity overlap with the
  legacy render is zero. The separately managed Namespace manifest contains
  exact Ambient and Pod Security labels and no Helm ownership annotations.
- Cutover render keeps all seven legacy Deployments and seven Services and
  resolves both MCP targets through `*.ewallet-mcp.svc.cluster.local:8080`.
- Credential dry-run targets seven client/revocation Secrets in
  `ewallet-agents` and two server tables in `ewallet-mcp`; it reads the first
  predecessor from `ewallet-agents` and emits no token material.
- Python compilation and `git diff --check`: PASS.

Remaining blockers are intentionally runtime-only: no namespace/release or
Secret exists from this task; effective additive policies, controller RBAC
denial, endpoint readiness, token positive/negative cases, gateway routing,
dependency reachability, rollback and business E2E still require separately
approved R1A-S2 execution. The skipped Docker/model/G2 harness tests do not
establish those runtime facts.

## R1A-S2 forward order

Every step is a separate approval/checkpoint; do not combine them into one
unreviewed command.

1. Read back Git SHA, images, releases, all selecting policies and available
   node capacity. Confirm no kagent Role/RoleBinding targets `ewallet-mcp`.
2. Diff and apply `infra/kubernetes/llm-agent-mcp/namespace.yaml` as a
   platform-owned object. Read back its Ambient, Pod Security and
   `ewallet.io/lifecycle-owner=platform-operator` labels. Refuse installation
   if it already has Helm release ownership annotations or conflicting labels.
3. Render/diff `llm-agent-mcp` with `mcp-isolated.yaml`; verify the render has
   no Namespace or Secret object before installation.
4. Dry-run the credential plan. With explicit approval, prepare client tokens
   in `ewallet-agents` and transition auth tables in `ewallet-mcp`, reading the
   legacy tables from `ewallet-agents`.
5. With separate approval, provision the exact Redis connection Secret copy
   and checksum-pinned registry ConfigMaps in `ewallet-mcp`.
6. Install `llm-agent-mcp`; do not change the existing `llm-agent` release.
   Verify both MCP Deployments, Services, endpoints, probes and auth-negative
   cases. `feature-rag-mcp` has no PDB; `drift-mcp` retains `minAvailable: 1`.
7. Verify gateway-origin ingress, direct-specialist denial, discovery-only
   denial of tool calls, scoped Redis/KServe access, and no drift backend
   access. Record status codes only; never print tokens.
8. Render/diff and apply `mcp-cutover.yaml` to `llm-agent`. Confirm both legacy
   Deployments and Services remain in its manifest. Verify the gateway routes
   to isolated endpoints.
9. Promote `clientRelease` only after server transition checks. Run one real,
   bounded business E2E and preserve logs/screenshots without secret material.
10. Do not finalize credentials or remove legacy resources. Those are later,
   independently approved gates.

## Rollback order

1. Restore the existing gateway backend namespace to `ewallet-agents`.
2. Restore `clientRelease=legacy` and verify RemoteMCPServer discovery.
3. Keep `serverSet=legacy` on legacy workloads; do not regenerate a token.
4. Confirm legacy `drift-mcp`/`feature-rag-mcp` Services and endpoints.
5. Roll back or uninstall `llm-agent-mcp` only after traffic is proven legacy.
6. Retain all legacy/transition Secrets. Deletion requires a later inventory
   review and explicit owner approval.
7. Do not delete `Namespace/ewallet-mcp` during rollback. Namespace and
   external Secret/runtime-input cleanup require a separate inventory and
   owner-approved platform operation.

## Planned live validation

R1A-S2 must prove controller `can-i` denials for Secrets and workloads in
`ewallet-mcp`, effective NetworkPolicy/Istio identities, gateway-only traffic,
positive and negative token grants, RemoteMCPServer discovery, dependency
reachability, rollback, and one bounded business E2E. Until then all runtime,
gateway, business and rubric statuses remain unverified.

## Proposed Living Decision addendum

**LD21.354 — G3 MCP namespace isolation source contract.** Retain official
kagent 0.10.3 and the single MCP Gateway in `ewallet-agents`. Introduce a
disjoint `llm-agent-mcp` release in `ewallet-mcp` for MCP server workloads,
server authentication tables and scoped runtime inputs. Client/discovery
credentials remain in `ewallet-agents` because official kagent resolves them in
the Agent/RemoteMCPServer namespace. Use immutable versioned credentials and a
blue/green cutover; legacy workloads and Secrets remain until rollback and
owner-approved cleanup complete. Source/static verification does not imply
runtime, business E2E or rubric verification.
