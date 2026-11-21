# G3-A1 campaign g3a1-20261121T175830Z: blocked before wake

Evidence class: GKE_PRIVATE read-only. **No mutation was made**: no CRD installed, no wake,
no Kubernetes object, no Cloud Lab execute. GKE stays at 0 nodes.

## Step 0 results

| Gate | Result |
|---|---|
| Git | PASS: branch `feature/edai2-external-gateway-api`, HEAD = remote = 94f28422, clean |
| GKE identity | PASS: cloud-lab kubeconfig server = cluster DNS endpoint, auth OK (v1.36.4-gke.2377000), current-context unchanged |
| Sleep state | PASS: cpu-pool 0 VMs, no drift since G3-A0 (no Gateway API CRDs, no LoadBalancer, no ewallet-ingress) |
| Recovery manifest | PASS: cicd-closure-20261121T093155Z, SHA256SUMS OK; `plan-wake --mode full` = 4 nodes (5 approved) |
| Cost baseline | asleep ~$0.11/h; 5 nodes ~$1.00/h + $0.10/h cluster fee (list-price estimate) |
| **Sleep feasibility** | **BLOCKED** (below) |

## Why the gate blocks

After `wake-full` the manifest restores three Deployments whose PDBs (minAvailable 1) block a
drain to zero (`00-preflight/sleep-feasibility.txt`):

| PDB | Replicas after wake | In approved record `sleep_20261106T163042Z` |
|---|---|---|
| ewallet-agents/fraud-coordinator | 2 | yes |
| ewallet-mcp/drift-mcp | 2 | yes |
| **ewallet-mcp/feature-rag-mcp** | 2 | **no** |

Without `--also-approve ewallet-mcp/feature-rag-mcp`, `cloud_lab.py sleep --scale-pdb-guarded`
refuses (`SLEEP_BLOCKED: PDB blockers not in the approved manifest`), so the campaign could not
end in guarded sleep within the approval. Per the G3-A1 directive (section 18.3), the run
stops before incurring cluster cost.

## Approval needed to continue

Exact exception: `--also-approve ewallet-mcp/feature-rag-mcp` for campaign `g3a1-20261121T175830Z` (sleep only).
Effect: before the resize, the tool scales Deployment `ewallet-mcp/feature-rag-mcp` 2 → 0
(feature/RAG MCP server; nothing else). The fresh recovery manifest recorded just before the
sleep restores it to 2 at the next wake. If the sleep fails after this, the tool restores the
replicas. This is the same exception the owner granted in the CI/CD closure and earlier
agent campaigns.
