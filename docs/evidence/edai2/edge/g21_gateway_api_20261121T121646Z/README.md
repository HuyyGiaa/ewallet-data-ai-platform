# G2.1 Gateway API foundation: local kind evidence (2026-11-21)

Local evidence only. GKE was not touched: it stayed asleep, nothing was applied there, and no
LoadBalancer, IP address, DNS record or certificate was created anywhere.

## Environment

| Item | Value |
|---|---|
| Context | `kind-llm-agent-local` (node `llm-agent-local-control-plane`), Kubernetes v1.37.0 |
| Istio | 1.30.7 (istio-base, istiod, istio-cni, ztunnel), GatewayClass `istio` (`istio.io/gateway-controller`) |
| Gateway API CRDs | v1.7.0, standard channel (already installed on the kind cluster) |
| Gateway workload | official `istio/gateway` 1.30.7 chart (sha256 `f8b350a2…e125f`), `values-gateway.yaml`, Service type overridden to NodePort for the test |
| Tools | helm v3.22.0, kubectl v1.37.1, kind v0.33.0, OpenSSL 3.5.3 |
| Backend | one echo stub (`scripts/edge/stub/echo_stub.py`) behind all four routes, in namespace `edge-stub` |

## Commands

```bash
# Static
helm lint infra/helm/ewallet-edge
helm template e infra/helm/ewallet-edge -n ewallet-ingress                    # default: renders nothing
helm template e infra/helm/ewallet-edge -n ewallet-ingress <all routes> --set domain=ewallet.example.com
                                                                              # refused: static/planned-unverified.err
kubectl --context kind-llm-agent-local apply --dry-run=server -f <render>     # test-domain and planned-verified renders
                                                                              # (ml-platform/ewallet-ingress mapped to default, which exists)
# Runtime
scripts/edge/local_validate.sh <dir> foundation
uv run --locked --project infra/docker/llm-agent-tooling --group test python -m pytest -q tests/ops/test_session_a_charts.py
```

## Files

| File | Content |
|---|---|
| `rendered.yaml` | the edge chart as applied in the run (`.test` domain, stub backends, `securityFiltersVerified=false`) |
| `status.jsonl` | 11 checks read from the API server and from the gateway's Envoy config: GatewayClass, Gateway, listeners, HTTPRoutes, ReferenceGrants, workload ownership, LoadBalancer count, Envoy route names, retries, TLS 1.1 refusal |
| `results.jsonl` | 27 HTTP checks through the gateway (`negative_tests.py --stub --foundation`) |
| `static/` | lint output, the refused real-domain render, the planned production render (with `securityFiltersVerified=true`, not applied anywhere) and the test-domain render |

Result: 11/11 status checks and 27/27 HTTP checks passed; `tests/ops/test_session_a_charts.py`
passed 26/26. Basic Auth (401) and rate limiting (429) were not tested: the EnvoyFilter is not
rendered in G2.1 and is verified in G2.2.

The run's cleanup removed both namespaces. Afterwards the cluster's Helm releases and Gateway
API, ReferenceGrant and EnvoyFilter resources matched the pre-run snapshot, and no
LoadBalancer Service existed.
