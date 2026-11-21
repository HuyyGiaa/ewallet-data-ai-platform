# G3-A1 plan after the G3-A0 closure (for owner approval)

Nothing below has run. Every step is a GKE mutation that needs the owner's explicit
approval. Evidence class of this campaign: LOCAL_KIND (tests, renders, the staged-rollout
check) plus GKE_PRIVATE read-only lookups (CRD conflicts, sleep record).

## Inputs, pinned

| Input | Value | Check |
|---|---|---|
| Source | `feature/edai2-external-gateway-api` at the closure commit | clean tree |
| Recovery manifest | `~/ewallet-evidence/cicd/cicd-closure-20261121T093155Z/recovery-manifest` (2026-11-21T09:33:32Z, 82 workloads, 5 VMs) | newest of the four manifests on disk; campaign `SHA256SUMS` OK |
| Gateway API | v1.7.0 `standard-install.yaml`, sha256 `4e2602c4cbe1afbc394d86f4ac6522e1a5263b01133bba1e40a6d1a549e753a1` | equals the GitHub release asset digest and the G2.1 closure input. 10 CRDs + ValidatingAdmissionPolicy and binding `safe-upgrades.gateway.networking.k8s.io`; none exists on GKE |
| istio/gateway | 1.30.7, chart sha256 `f8b350a2…e125f`, `values-gateway.yaml` | ClusterIP, 1 replica, no HPA, no PDB |
| ewallet-edge | repository chart, `g3a-private-values.yaml` (`edge.g3a.test`, `securityFiltersVerified: true`, 4 routes) | 16 objects; `ownership_preflight render`: EDGE_OWNERSHIP_OK |

## Order (fail-closed)

1. **CRDs while asleep:** `kubectl --kubeconfig <cloud-lab> --context <gke> apply --server-side -f standard-install.yaml`, then wait for `Established`. istiod then starts with the CRDs at wake, the order G2 tested.
2. **Wake:** `CLOUD_LAB_APPROVED=yes cloud_lab.py wake-full --manifest <cicd-closure>/recovery-manifest --nodes 5 --execute --confirm-cluster ewallet-dev`. It unpauses the cicd-closure KEDA objects. Then check: nodes Ready, Pending Pods, istiod GatewayClasses, the four backends Ready, Valkey and kagent-controller recovery, and drift against the manifest. Stop if unstable.
3. **Namespace and Secrets:** `ewallet-ingress`; `edge-tls` (1-day self-signed ECDSA, SAN `*.edge.g3a.test`; CA kept outside the repo); `edge-basic-auth` via `create_basic_auth.sh` (random, stdin only).
4. **Gateway workload at 0 replicas:** `helm install ewallet-edge-gateway istio/gateway --version 1.30.7 -n ewallet-ingress -f values-gateway.yaml --set replicaCount=0`. No Envoy is running yet.
5. **Edge chart:** `helm install ewallet-edge infra/helm/ewallet-edge -n ewallet-ingress -f g3a-private-values.yaml`. Wait for the Gateway `Accepted=True` and every HTTPRoute `Accepted`/`ResolvedRefs=True`. `Programmed` stays False (AddressNotAssigned) with no pods, as on kind.
6. **Start the gateway:** `helm upgrade ewallet-edge-gateway … -f values-gateway.yaml` (1 replica). The first Envoy starts from a snapshot that already holds the EnvoyFilter. On kind its first config had `local_ratelimit → basic_auth → router` on 443 (`staged-rollout/result.txt`).
7. **Verify before any test:** config dump (filters on 443, none on 80, per-route buckets, no retry policy, route names), Gateway `Programmed`, live ownership preflight, no LoadBalancer. kagent-ui must stay Ready under the new policy; this is the Calico check kind cannot do.
8. **Tests** (PA-01..25, RB-01..04): over port-forward with `--resolve` and `--cacert`, `negative_tests.py` without `--stub`. Read-only on the backends.

If step 7 fails, roll back before any backend test.

## Rollback (campaign-owned only)

`helm uninstall ewallet-edge`, then `helm uninstall ewallet-edge-gateway`, then delete the
`ewallet-ingress` namespace. The objects in backend namespaces carry the edge release
labels and go with the release. Keep the Gateway API CRDs. If a backend regresses (for
example kagent-ui NotReady), roll back first, record it, then sleep.

## End state (recommended: keep the private edge, then guarded sleep)

At sleep time, not before:

1. `cloud_lab.py record-manifest --out <new campaign>/recovery-manifest --campaign <id> --source-commit <commit>` on the healthy, awake cluster. This fresh manifest includes the edge gateway Deployment.
2. `cloud_lab.py plan-sleep --manifest docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z`. That approved record covers `ewallet-agents/fraud-coordinator` and `ewallet-mcp/drift-mcp`. The actual `pdb_blockers`, `not_covered` and `keda_pause` lists printed then are what the owner approves. The last sleep showed three blockers; `ewallet-mcp/feature-rag-mcp` was not covered.
3. `cloud_lab.py sleep --scale-pdb-guarded --pause-keda --manifest <that record> --campaign <id> [--also-approve <only the not_covered keys the owner names>] --execute --confirm-cluster ewallet-dev`, with `CLOUD_LAB_APPROVED=yes`, `CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:<id>` and `CLOUD_LAB_KEDA_PAUSE_APPROVAL=ewallet-dev:<id>`, scoped to that command.
4. `cloud_lab.py verify --expect sleep --manifest <fresh manifest>`.

The edge adds no PDB and no ScaledObject, so it does not change the sleep blockers. With
the closure fix, a refused sleep changes nothing, and a failure after preparation (also a
failed resize) restores the pauses and replicas and reports any step it could not undo.
