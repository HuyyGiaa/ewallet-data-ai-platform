# G3 Coordinator reliability — source-only architecture decision

## Decision

Select a bounded programmatic Coordinator implemented as an officially
supported kagent 0.10.3 BYO Agent.  Keep both Specialists declarative and keep
all delegation on the existing secured A2A Gateway.

The feasibility prototype is source-only.  It does not replace the deployed
Coordinator, render a Helm release, build an image or mutate GKE.

## Provenance

- Worktree: `/home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/codex-llm-g2`
- Branch: `codex/llm-g2-llmd-routing`
- Starting HEAD: `181d3ae68656ce0099c3ef2a1fa17f1adaf00609`
- Starting tree: `e46133c9c349ccbe526d9c11e884327179bd413e`
- Pinned kagent tag `v0.10.3`: commit
  `8878c39ac64421cb665c8e62ecc6411dd12f2fc2`, tree
  `2c41e13a4c725c8f12f37dfdee9fab1644b0f804`
- Read-only live state: `llm-agent` revision 15, `llm-agent-mcp` revision 3,
  `kagent` revision 2.
- Canonical Living Decisions readback: LD21.373 and LD21.374 exist;
  LD21.375 is absent.

## What the previous diagnostic proves

It proves that kagent registered both Agent tools and that the current serving
route can emit a structured tool call in a simplified case.  It also proves
that the full Coordinator case returns pseudo-tool text rather than a native
call and that forced selection does not preserve the nested request reliably.

It does not prove a live worker request body, kagent dispatch failure or an A2A
Gateway failure.  The request was reconstructed from pinned source and live
configuration, not captured transparently from the worker.

## Option A — declarative Coordinator

Pinned source sets `ChatCompletionNewParams.Tools` and hard-codes
`ToolChoice=auto` whenever tools exist.  The Agent/ModelConfig CRDs expose no
per-request tool-selection policy.  The remote Agent tool schema has one
required free-form string parameter named `request`; it does not bind the G3
typed transaction schema.  Dispatch occurs only after the model emits a valid
function call, so malformed or pseudo-tool output cannot be made reliable by
the declarative layer.

Option A can reject invalid output after generation, but it cannot guarantee
selection or byte-exact transaction identity before dispatch without changing
the worker/model adapter.  Such a worker fork would broaden the change into
shared serving and is not the smallest supported correction.  Option A is
therefore infeasible for the current reliability requirement.

## Option B — kagent BYO Coordinator

The pinned `Agent` CRD explicitly supports `spec.type: BYO`.  Kagent owns its
Deployment, Service and ServiceAccount, expects an A2A server on port 8080,
publishes an Agent Card, proxies it through `/api/a2a/{namespace}/{name}`, and
keeps the same UI path.  The official Go app builder also wires controller task
and session stores when `KAGENT_URL` is present.  A BYO agent therefore remains
a first-class kagent/A2A agent rather than a new public application.

The selected flow is:

```text
validated inbound A2A message
  -> G3BusinessRequest validation and deterministic capability routing
  -> existing A2A Gateway with exact x-kagent-host allowlist
  -> existing declarative Specialist
  -> existing authenticated MCP Gateway
  -> Feast / KServe / RAG
  -> terminal Specialist evidence validation
  -> bounded LLM decision-support text
  -> programmatic assembly preserving business evidence byte-for-byte
```

The existing `fraud-coordinator` ServiceAccount name must be retained so Istio
continues to enforce the existing SPIFFE principal.  No direct Specialist URL
is accepted by the prototype.  Session and request ID form the idempotency
scope.  Only a completed A2A task with exactly one bounded JSON artifact is
accepted.

The current prototype is the transport-neutral core plus a bounded real JSON
HTTP adapter.  The deployment campaign must still wrap it in the pinned
kagent BYO A2A server/executor, controller-backed task/session persistence and
OTel spans before runtime promotion.

## Decision matrix

| Dimension | Declarative | Programmatic BYO |
|---|---|---|
| Correctness | Fails demonstrated full prompt | Deterministic routing and validation pass locally |
| Argument fidelity | Model-controlled free-form string | Typed request copied by code |
| Native A2A | Yes | Yes, through official BYO A2A contract |
| Rubric equivalence | Native Agent-as-Tool, but runtime blocked | First-class kagent Agent; requires runtime/UI evidence |
| Security | Existing identity/policies | Preserves same SA/Gateway; implementation must not add an endpoint |
| Runtime complexity | Lowest | One small custom worker image |
| Resource cost | Existing 100m/256Mi request | Target same or lower; measure before claim |
| Integration risk | Low but unreliable | Medium; bounded to Coordinator resource/image |
| Rollback | Prompt/CR revision | Replace BYO Agent with revision-15 declarative Agent |
| Maintainability | Upstream-managed | Project owns core and adapter |
| Reproducibility | Model-dependent failure | 21 focused and 209 affected tests pass |

## Prototype contracts

Changed source:

- `ai_platform/llm_agent/programmatic_coordinator.py`
- `tests/llm_agent/test_g3_programmatic_coordinator.py`

The prototype covers:

1. transaction identity preservation;
2. missing ID and literal placeholder rejection;
3. malformed nested payloads and unsupported capabilities;
4. explicit fraud/drift routing and no unsolicited drift;
5. exact A2A JSON-RPC `message/send` semantics;
6. exact approved Gateway URL and Specialist header allowlist;
7. backend value and RAG citation preservation;
8. timeout/cancellation and completed-task enforcement;
9. session isolation and idempotency conflict detection;
10. direct backend/credential-bearing URL denial;
11. bounded input, response and synthesis sizes;
12. pseudo-tool text rejection and non-fabrication of business fields.

## Acceptance evidence

Commands used from the worktree:

```bash
UV=/home/huyy-giaa/miniconda3/envs/fintech/bin/uv
cd infra/docker/llm-agent-tooling
"$UV" lock --check
"$UV" sync --locked --all-extras

cd /home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/codex-llm-g2
"$UV" --project infra/docker/llm-agent-tooling run --locked --all-extras \
  python -m unittest tests.llm_agent.test_g3_programmatic_coordinator -v
```

Results:

- `uv lock --check`: PASS, 120 packages resolved with no lock change.
- `uv sync --locked --all-extras`: PASS.
- Programmatic Coordinator: 21/21 PASS.
- Affected G3/MCP/A2A/chart suite: 209/209 PASS in 8.207 seconds.
- Python compilation: PASS.
- `git diff --check`: PASS.

One intermediate regression run after default `uv sync --locked` had one
environment error because optional Feast packages were intentionally absent.
After the locked extras were restored, the affected group passed completely.
This was not a source regression.

## Smallest deployment plan — not executed

1. Add one private Coordinator image containing the official pinned kagent BYO
   A2A server adapter, this prototype core and the locked project environment.
2. Render the existing `Agent/ewallet-agents/fraud-coordinator` as `type: BYO`
   with the same name, labels, ServiceAccount `fraud-coordinator`, resource
   requests/limits, node selector and port 8080.  Do not create another public
   Service or Gateway.
3. Keep the two Specialist Agent resources, A2A Gateway routes,
   AuthorizationPolicies, NetworkPolicies, MCP Gateway and MLOps resources
   byte-for-byte unchanged.
4. Pin the image by digest.  Verify rendered diff is limited to the Coordinator
   Agent and its resulting kagent-owned Deployment pod template.
5. Validate Agent Card/UI, controller-backed sessions/tasks, OTel trace
   propagation, exact positive/negative Gateway tests and one bounded business
   E2E before promotion.
6. Roll back by restoring the exact revision-15 declarative Coordinator Agent
   specification/image and verifying A2A/MCP/MLOps health.  No credential,
   policy or data rollback is required.

## Remaining gates

```text
OPTION_A_DECLARATIVE_RELIABILITY=INFEASIBLE_ON_PINNED_API
OPTION_B_BYO_SOURCE_FEASIBILITY=PASS
BYO_A2A_SERVER_ADAPTER=NOT_IMPLEMENTED
BYO_GKE_RUNTIME=NOT_DEPLOYED
COORDINATOR_BUSINESS_E2E=BLOCKED
RAG_AIRFLOW_INGESTION=PASS_REPORTED
RAG_DB1_READBACK=PASS_REPORTED
RAG_PRODUCTION_RETRIEVAL=PASS_REPORTED
RAG_DB1_TO_PRODUCTION_READ_AFTER_WRITE=NOT_PROVEN
DATAHUB_PUBLICATION_READBACK=BLOCKED
FULL_BUSINESS_E2E=BLOCKED
RUBRIC_VERIFIED=NO
```

No DataHub stack, RAG pipeline, Redis DB, Helm release, image registry, IAM,
RBAC, NetworkPolicy, model, parser or chat template was changed.
