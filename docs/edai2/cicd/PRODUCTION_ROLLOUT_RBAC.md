# Jenkins production rollout: least-privilege design

This is a design only. **Nothing here is applied**, and Jenkins can't reach production today. A live check on 2026-11-06 confirmed that 34 of 34 matrix rows match that state.

Files: [the proposed Roles](../../../infra/kubernetes/jenkins/production-rollout-rbac.proposed.yaml), [the matrix](../../../jenkins/cd/production-rbac-matrix.json) and [the checker](../../../scripts/ci/rbac_matrix.py). Tests prove the Roles grant exactly what the matrix says.

## Approach

Jenkins would patch one image digest on named objects, and it would not run `helm upgrade`.

Helm stores release state in Secrets, and Kubernetes can't limit `create` by object name. A Helm-based deploy would need to read and write every Secret in `default` and `ewallet-mcp`, which is broad namespace ownership. A digest patch needs no Secret access.

The cost is drift: Helm no longer knows the live digest. The owner has to carry each released digest into the release values, or the next `helm upgrade` silently rolls it back.

## Permissions

The subject is `system:serviceaccount:ewallet-ci:jenkins`.

| Component | Namespace | Resource | Names | Verbs |
|---|---|---|---|---|
| MCP | `ewallet-mcp` | `deployments.apps` | `feature-rag-mcp`, `drift-mcp` | get, list, watch, patch |
| MCP | `ewallet-mcp` | `replicasets.apps`, `pods` | any | get, list, watch |
| Agents | `ewallet-agents` | `agents.kagent.dev` | `fraud-coordinator`, `feature-rag-specialist` | get, list, watch, patch |
| Agents | `ewallet-agents` | `deployments.apps` | the same two | get, list, watch |
| Agents | `ewallet-agents` | `replicasets.apps`, `pods` | any | get, list, watch |
| Streaming | none | none | none | none |

- **Streaming has no role**, because no streaming workload runs on GKE.
- **`drift-specialist`** is excluded: it's Declarative and has no image.
- **`drift-mcp` in `ewallet-agents`** is excluded: the gateway routes to the copy in `ewallet-mcp`, so that Deployment looks unused. Confirm before removing it.

Never granted: Secrets, exec, port-forward, create, delete, RBAC objects, cluster scope, other namespaces.

## Residual risk

`patch` on a Deployment or Agent isn't limited to the image field. A compromised Jenkins could change a command or environment on these four objects and run code as that workload.

Kubernetes RBAC can't express a narrower rule. A `ValidatingAdmissionPolicy` that allows only the image field to change would close this, but it's cluster-scoped, so it needs a separate decision.

## Check

`kubectl auth can-i` is read-only.

```bash
python scripts/ci/rbac_matrix.py --context "$KUBE_CONTEXT" --state before   # now: every right absent
python scripts/ci/rbac_matrix.py --context "$KUBE_CONTEXT" --state after    # after approval: exact match
```

## Apply and roll back

Both need owner approval.

```bash
kubectl --context "$KUBE_CONTEXT" apply -f infra/kubernetes/jenkins/production-rollout-rbac.proposed.yaml
kubectl --context "$KUBE_CONTEXT" delete -f infra/kubernetes/jenkins/production-rollout-rbac.proposed.yaml
```

Rolling back removes the rights at once and leaves running workloads as they are. To restore an image, patch the previous digest back or run `helm rollback`.

The rollout step itself isn't implemented, because no RBAC exists for it to use.
