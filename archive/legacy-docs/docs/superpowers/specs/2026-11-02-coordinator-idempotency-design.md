# Coordinator durable idempotency design

## Intent

Prevent a replayed G3 business request from executing the Specialist/MCP path
again when the request arrives with a new A2A context or at another Coordinator
replica. Preserve the existing G3 business contract, Agent Cards, gateways,
Feast, KServe and RAG behavior.

## Constraints

- The production authority cannot be process-local state or the SDK context ID.
- State is scoped by a source-configured tenant, the Istio-authorized kagent
  controller principal and the canonical business `request_id`.
- The canonical request JSON is SHA-256 fingerprinted; reuse of an identity with
  different input fails closed.
- Redis is dedicated to Coordinator idempotency. It does not share data,
  credentials or lifecycle with Feast/RAG Redis.
- No exactly-once downstream guarantee is claimed across an ambiguous crash
  window.

## Chosen approach

Use a dedicated, authenticated Redis StatefulSet with AOF, a private ClusterIP,
a dedicated PVC and NetworkPolicy limited to the Coordinator. Atomic Lua
scripts implement claim, delegation-start, completion and failure transitions.

Alternatives rejected:

- KAgentTaskStore lacks conditional create/CAS for a stable business key.
- Kubernetes Lease/ConfigMap would turn control-plane etcd into an application
  result store and require broader RBAC.
- Feast Redis does not provide the required lifecycle and credential isolation.

## State machine

`ABSENT -> CLAIMED -> STARTED -> COMPLETED`

- `CLAIMED`: one owner token/generation holds a bounded lease. A stale claim may
  be recovered only before delegation has started.
- `STARTED`: downstream execution may have occurred. A stale lease becomes
  `AMBIGUOUS`; it is never automatically re-executed.
- `COMPLETED`: the validated result is replayed without Specialist/MCP calls and
  expires after the configured completed-result TTL.
- `AMBIGUOUS`: fail closed and require explicit operator reconciliation. A stale
  owner cannot overwrite it or a newer completion.

Concurrent identical requests poll for a bounded interval. They replay a result
that becomes complete; otherwise they receive an explicit in-progress failure.
Redis errors and timeouts fail closed.

## Identity and trust

The runtime tenant and service principal are immutable Helm values matching the
existing Istio AuthorizationPolicy for the kagent controller. Forwarded
`x-user-id` is not treated as the authentication authority. Core interfaces
still accept an explicit scope so tenant/principal isolation is regression
tested without relying on an HTTP header.

## Runtime resources

The chart will render one Redis StatefulSet, ClusterIP Service, PVC template and
NetworkPolicies. Credentials are supplied by a pre-created Secret and never
rendered into values or evidence. The Coordinator gets only the Secret key and
network path required for this backend.

## Verification

Tests first reproduce different-context, different-replica, concurrency,
completed replay, conflict, outage, stale claim, crash ambiguity and scope
isolation. Runtime rollout then validates one replica, multiple replicas, MCP
invocation counts, HPA scale-out/in and the existing G3/security path before any
Substrate work.
