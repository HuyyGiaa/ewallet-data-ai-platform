# Jenkins production rollout

**Jenkins gets no production rights.** Production RBAC and rollout are not approved, and this design makes them unnecessary. Jenkins can't reach production today: a live check on 2026-11-06 confirmed that 34 of 34 matrix rows match that state.

## Recommended path: promotion through Helm

The earlier proposal let Jenkins patch digests directly. Owner review rejected that until two problems were solved. RBAC can't limit a `patch` to the image field, and a direct patch leaves Helm's release record out of date, so the next `helm upgrade` silently rolls the image back.

Both go away if Helm stays the only writer:

1. Jenkins runs CD in `ewallet-cicd`, then publishes a promotion file holding only digests.
2. The approved deployer runs `scripts/ci/release_state.py promote` with their own credentials.
3. The tool refuses unless every check below passes, and upgrades with `--atomic`.

| Problem | Check | Tested on production data, read-only |
|---|---|---|
| Running image differs from Helm's record | `drift` compares both | 0 differences on `llm-agent` and `llm-agent-mcp` |
| Local chart isn't what's deployed | Render must equal `helm get manifest` | Both releases match exactly |
| A release changes more than images | Before and after renders may differ only in image identity | A real digest change passes; a smuggled replica change is refused |

Image-only is enforced by comparing renders, which is stronger than any RBAC rule. Helm's record stays true because Helm does the write, and `--atomic` restores the previous revision on failure.

A release that changes the chart fails the check on purpose. It's a normal reviewed upgrade, not a promotion.

```bash
python scripts/ci/release_state.py drift --release llm-agent --context "$KUBE_CONTEXT"
python scripts/ci/release_state.py values --release llm-agent --image coordinator=<digest-ref> --git-sha <sha> --output promotion.yaml
python scripts/ci/release_state.py check --release llm-agent --context "$KUBE_CONTEXT" --promotion promotion.yaml
PROMOTION_APPROVED=yes python scripts/ci/release_state.py promote --release llm-agent --context "$KUBE_CONTEXT" --promotion promotion.yaml --execute
```

`drift`, `values` and `check` are read-only. `promote` is a cloud mutation and needs owner approval, and it has never run.

**Limit:** this keeps a person in the loop. If Jenkins must deploy unattended later, something has to hold Helm's rights, which means a new decision. A `ValidatingAdmissionPolicy` isn't needed for this path, so it stays deferred.

## Superseded proposal

The rest of this page describes the direct-patch proposal, kept for integration review. **It is not recommended and not approved.**

Files: [the proposed Roles](../../../infra/kubernetes/jenkins/production-rollout-rbac.proposed.yaml), [the matrix](../../../jenkins/cd/production-rbac-matrix.json) and [the checker](../../../scripts/ci/rbac_matrix.py). Tests prove the Roles grant exactly what the matrix says.

Helm stores release state in Secrets, and Kubernetes can't limit `create` by object name, so giving Jenkins Helm itself would need read and write on every Secret in `default` and `ewallet-mcp`.

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

No rollout step exists for these Roles, and none is planned while the promotion path stands.
