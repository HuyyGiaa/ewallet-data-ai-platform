# G2.1 closure: Gateway API compatibility and rollout-safety evidence (2026-11-21)

All runs are local. GKE was not touched: no kubectl or gcloud call was made against it during the closure, and it stayed asleep.

## Compatibility matrix: Istio 1.30.7 on three Gateway API CRD bundles

Each case ran on its own throwaway kind cluster (`g21-gw151`, `g21-gw151x`, `g21-gw170`): kindest/node v1.37.0, istio/base and istio/istiod 1.30.7. The cases ran one at a time, and each cluster was deleted afterwards. `compat_case.sh.txt` is the exact script. `inputs.txt` lists the CRD bundle checksums (official release assets) and the Gateway API version pinned in Istio 1.30.7's `go.mod`.

| Item | v1.5.1 Standard (`case-a-v151`) | v1.5.1 Experimental (`case-a2-v151x`) | v1.7.0 Standard (`case-b-v170`) |
|---|---|---|---|
| `HTTPRoute.rules[].retry` in the CRD schema | absent | present | present |
| `kubectl apply` (strict field validation) | **rejected**: `unknown field "spec.rules[0].retry"` | accepted, stored | accepted, stored |
| Non-strict client (`--validate=false`) | **silently pruned** | stored | stored |
| Chart CRD guard (`helm install --dry-run=server`) | **refuses** | passes | passes |
| GatewayClass `istio` | Accepted | Accepted | Accepted |
| Gateway, listeners, HTTPRoutes, ReferenceGrants | not reached (apply refused) | Accepted, Programmed, ResolvedRefs | Accepted, Programmed, ResolvedRefs |
| Workload ownership, ClusterIP/NodePort, no LoadBalancer | not reached | 1 Helm-owned Deployment and Service | 1 Helm-owned Deployment and Service |
| Envoy `retry_policy` on the edge routes | `num_retries: 2` when the guards are bypassed (`pruned-results.jsonl`) | none | none |
| Backend attempts per client request: 200, 503, reset, timeout | 1, 1, 1, 1 (bypassed guards) | 1, 1, 1, 1 | 1, 1, 1, 1 |
| Foundation validation | fails closed at apply | 12/12 status, 30/30 HTTP | 12/12 status, 30/30 HTTP |

**Interpretation**

- Istio 1.30.7 is built against Gateway API v1.5.1. Its 1.30 upgrade notes require "v1.5.x" CRDs. In the v1.5.1 Go types `retry` is an experimental field, and Istio converts it into the VirtualService retry policy (`pilot/pkg/config/kube/gateway/conversion.go`, tag 1.30.7).
- Without the field, Istio gives every route `num_retries: 2` with `retry_on: connect-failure,refused-stream,unavailable,cancelled,retriable-status-codes`. That list names no retriable status codes. So a backend 503, a connection the backend drops after reading the request, and a timeout were each attempted once even then. This corrects a G2.1 statement that the default also retries 503.
- With v1.5.1 Standard the declared no-retry contract cannot be stored. The default policy still retries connection-level failures. The chart and `kubectl` therefore refuse that combination instead of deploying a route whose Envoy config differs from its manifest.
- v1.7.0 Standard is newer than the version Istio 1.30.7 is tested with. Istio's documented requirement is the v1 `ReferenceGrant` and `TLSRoute`, which v1.7.0 also serves, with the same storage versions as v1.5.1. In these runs every resource the edge uses reconciled on v1.7.0 on two independent clusters: this matrix and the long-lived `kind-llm-agent-local` (`regression-llm-agent-local-v170`).

## Compatibility decision record

| Item | Result |
|---|---|
| Istio version | 1.30.7 (`go.mod`: `sigs.k8s.io/gateway-api v1.5.1`) |
| Gateway API versions tested | v1.5.1 Standard, v1.5.1 Experimental, v1.7.0 Standard |
| Schema compatibility | v1.5.1 Standard lacks `retry`. The other two keep it |
| Controller compatibility | Istio 1.30.7 reconciled Gateway, HTTPRoute (with `retry`), ReferenceGrant and GatewayClass on v1.5.1 Experimental and v1.7.0 Standard |
| Retry behavior | One backend attempt per request on both, verified with the stub's per-request counter and Envoy config dump |
| Recommended G3 CRD version | **v1.7.0 Standard.** Fallback: v1.5.1 Experimental (documented by Istio 1.30, but installs experimental APIs cluster-wide). Not v1.5.1 Standard: the chart refuses it |
| Remaining risk | v1.7.0 is outside Istio 1.30.7's tested matrix. Re-check on the first istiod upgrade, and run the chart guard and `ownership_preflight.py live` in G3 |

## Other runs

- `regression-llm-agent-local-v170/`: the full foundation run on the existing `kind-llm-agent-local` cluster, which already had v1.7.0 installed; its CRDs were not changed. Campaign namespaces were created and removed.
- The legacy Gateway guard (`scripts/edge/ownership_preflight.py`): `render` rejects `infra/helm/llm-agent-platform/values-gcp.yaml` (unexpected ingress Gateway, not bound to the Helm Service). `live` returned `EDGE_OWNERSHIP_OK` in every run above. Its tests are in `tests/ops/test_session_a_charts.py`.

Note: `case-a-v151/pruned-results.jsonl` shows one failed check, "plain HTTP redirects to HTTPS: 0". It is an artifact of the experiment: the measurement passed port 80 instead of the node port. The redirect itself passed in every real run.
