# M2 review: pod-specific NetworkPolicy for `ewallet-prompts`

Status: **design review and local test only. The production NetworkPolicy is unchanged.** This review is for owner decision 4.

Evidence: `docs/evidence/edai2/cicd_runtime_20261103/registry-security/m2-kind/`.

## 1. Existing vs proposed

Diff: `m2-kind/policy-diff.txt`. Source: `infra/kubernetes/prompt-registry/hardening-m1-m2-proposal.yaml`, sha256 `b121790d…77345`.

| Policy | Live GKE (2026-11-03) | Proposed M2 |
|---|---|---|
| `default-deny` (all pods, Ingress+Egress) | present | **kept** |
| `same-namespace-and-dns` (all pods) | ingress from any pod in the namespace; egress to any pod in the namespace, plus DNS to `kube-system` and `34.118.224.10` | **deleted** |
| `prompt-registry-server` (`app.kubernetes.io/name=agentregistry`) | — | ingress: none; egress: DB pod on 5432, DNS to `kube-system` and `34.118.224.10` |
| `prompt-registry-db` (`app.kubernetes.io/name=prompt-registry-db`) | — | ingress: registry pod on 5432 only; egress: none |

## 2. Test environment and fidelity

- **Cluster:** kind `llm-agent-local`, kind v0.33.0 with kindnet. Its built-in network-policy controller **enforces**: a pod in a namespace with no policies at all is BLOCKED from the registry under the current policy.
- **Policies:** `ewallet-prompts` on kind holds policies byte-identical to GKE (spec JSON `cmp` equal). It also has the same Pod Security level (restricted), quota and LimitRange.
- **Workloads:** the same Postgres manifest (digest-pinned) and the same chart 0.4.0 values and server digest as production. The storage class is `standard` instead of `standard-rwo`, and the DB Secret is throwaway.
- **Probe sources:**

| Source | Purpose |
|---|---|
| `open` | A namespace with **no** policies, so ingress is tested alone |
| `cicd` | `ewallet-cicd`, the same as on GKE |
| `lateral` | A new pod inside `ewallet-prompts`, standing in for a compromised or unexpected pod |
| `db` | The Postgres pod |
| `registry` | The registry pod |

- **Difference from GKE:** GKE uses **Calico** (`networkPolicy.provider=CALICO`). Both engines implement standard `networking.k8s.io/v1` semantics: additive allow-lists, evaluation after Service DNAT, and no mediation of port-forward. The CNI-specific points are in §5.

## 3. Connectivity matrix

Four phases:

1. baseline = current policies
2. transition = M2 added while the old policy still exists
3. M2 = old policy deleted
4. rollback = old policy re-applied, then M2 deleted

| Source → target | 1 baseline | 2 transition | 3 M2 | 4 rollback |
|---|---|---|---|---|
| open / cicd → registry svc 12121, pod 8080, MCP 31313, DB svc/pod 5432 | BLOCKED | BLOCKED | BLOCKED | BLOCKED |
| **lateral → registry 12121 / 8080 / 31313** | REACHABLE | REACHABLE | **BLOCKED** | REACHABLE |
| **lateral → DB 5432** (svc and pod) | REACHABLE | REACHABLE | **BLOCKED** | REACHABLE |
| lateral → DNS | resolves | resolves | **no DNS** | resolves |
| **DB → registry 12121 / 8080 / 31313** | REACHABLE | REACHABLE | **BLOCKED** | REACHABLE |
| DB → DNS | resolves | resolves | **no DNS** (not needed) | resolves |
| registry → DB 5432 (svc and pod) | REACHABLE | REACHABLE | REACHABLE | REACHABLE |
| registry → DNS | resolves | resolves | resolves | resolves |
| registry → own Service 31313 (hairpin) | REACHABLE | REACHABLE | BLOCKED (not needed, see §4) | REACHABLE |
| any prompts pod → kube API 443, external 1.1.1.1:443 | BLOCKED | BLOCKED | BLOCKED | BLOCKED |

- **The transition phase equals the baseline.** Policies are additive, so adding M2 before deleting the old policy causes no interruption.
- **The rollback phase equals the baseline exactly** (`diff` empty). The policy set returns to the GKE state (`cmp` equal).

## 4. Functionality under M2

Tested on kind (`matrix-3-m2.txt`, `resilience-m2*.txt`).

| Check | Result |
|---|---|
| Operator REST through port-forward (`/v0/health`) | 200 |
| Operator publish (guarded `registry_catalog publish`, M3 pre and post gates) | PASS, 17/17 readback |
| M3 gate `post` | PASS |
| MCP `initialize` and `tools/call list_prompts` through port-forward | OK (`agentregistry-mcp v0.4.0`, prompts listed) |
| Registry pod restart (startup DB connection through DNS) | Ready, 0 restarts; gate PASS |
| DB pod restart (registry reconnect) | gate PASS on the first try |
| Readiness and liveness | registry `tcpSocket` probes and DB `exec` probe stay Ready in every phase |

One transient failure after the registry restart was traced to `kubectl port-forward` exiting while the old pod terminated. It was not caused by policy: the repeat on the same policies passed (`resilience-m2-registry-restart-retry.txt`).

## 5. Read/write exposure and alternate paths

The registry API has no authentication (F1/F2). Who can reach the write API afterwards:

| Path | Today (after M1) | After M2 | Control |
|---|---|---|---|
| Any pod in another namespace | blocked | blocked | NetworkPolicy |
| A pod inside `ewallet-prompts` (new, compromised or label-less) | **read and write** registry; **connect to Postgres** | blocked | NetworkPolicy (M2) |
| Postgres pod → registry | reachable | blocked | NetworkPolicy (M2) |
| A pod that **spoofs** `app.kubernetes.io/name=agentregistry` | — | inherits the registry's DB egress and DB ingress | Requires `create pods` in `ewallet-prompts`: only cluster-admin, built-in controllers, `ate-system/ate-controller`, the cluster autoscaler and GKE webhooks (`rbac-alternate-paths-gke.txt`); Jenkins: no |
| `kubectl port-forward` (kubelet → pod loopback) | cluster-admin, GKE `system:resource-tracker` | **unchanged**: not mediated by any NetworkPolicy, on Calico or kindnet | RBAC; Jenkins removed by M1 |
| `kubectl exec` into the registry pod (image has bash and curl) | `system:masters` only | unchanged | RBAC |
| Node or host-network processes on the registry's node | allowed (Calico allows host → local pod so that kubelet probes work) | unchanged | Node trust boundary; the current default-deny already relies on it (probes work today) |
| NodePort or LoadBalancer | none (quota `services.nodeports=0`, `loadbalancers=0`) | unchanged | ResourceQuota |
| Additive policies | any new policy that selects these pods re-opens access | same | Change review; the rollout post-check lists exactly 3 policies in the namespace |

**Constraint:** `ewallet-prompts` is **not** in the Istio ambient mesh (no `istio.io/dataplane-mode` label). If it were enrolled, pod traffic would arrive as HBONE on port 15008 and the port-specific M2 rules would break registry → DB. Enrolling the namespace would need a revised policy.

## 6. Verdict and rollout proposal (not executed)

M2 closes F5 (in-namespace lateral reach to the unauthenticated write API and to Postgres). Nothing needed is lost: the operator path is port-forward, and the only data path is registry → DB. Rollback is exact.

Residual risk after M1+M2 rests on two things:

- **RBAC:** cluster-admin port-forward and exec, and pod creation in the namespace.
- **Node trust.**

Both are outside NetworkPolicy, and M3 detects any tampering that results.

Proposed GKE steps, if the owner approves:

1. Pre-checks: `verify-all` / gate `post` 12/12. Policy set equals `live-gke-policies.json`.
2. `kubectl apply` the two M2 policies only, with the old policy still present. Re-run gate `post` (operator port-forward).
3. `kubectl delete networkpolicy same-namespace-and-dns -n ewallet-prompts`.
4. Post-checks:
   - registry and DB Ready
   - gate `post` PASS
   - registry pod restart, then gate PASS
   - in-namespace probe pod: the quota allows it, but it is not required; the kind matrix covers it
5. Rollback: re-apply `same-namespace-and-dns` from `live-gke-policies.json`, then delete the two M2 policies.

There is no G3 impact: no G3 component runs in or calls `ewallet-prompts`, and production r32 reads its prompt from a Git ConfigMap.
