# External edge gateway

This page describes the external gateway for EDAI-2 rows 37 to 43. It runs on Istio, is validated on real Envoy in kind, and is **not deployed**. No domain, certificate or public address exists yet.

| Status | State |
|---|---|
| `IMPLEMENTED` | Yes |
| `LOCAL_VALIDATED` | Yes: real Istio 1.30.7 gateway, 34 of 34 checks, stub backend |
| `CLOUD_DEPLOYED` | No |
| `CLOUD_RUNTIME_VERIFIED` | No |
| `RUBRIC_EVIDENCE_COMPLETE` | No |

## Why not the existing `gateway.yaml`

The cluster has no Gateway API CRDs, no GatewayClass and no ingress gateway, only `istiod`. The chart's old template could not have worked, and it names a Service, `fraud-coordinator-a2a`, that does not exist.

This design uses the official `istio/gateway` chart and Istio CRDs already installed: `Gateway`, `VirtualService`, `EnvoyFilter`. It installs no CRDs and no new controller.

The rubric title names NGINX. Reviewers should note this deviation: the requirements are met with Istio, because the platform already runs it.

## Routes

Every route is off by default. `<d>` is the owner's domain.

| Host | Paths | Upstream | Limit |
|---|---|---|---:|
| `features.<d>` | `POST /v1/features/lookup` | `feature-api.ml-platform:80` | 100/s |
| `fraud.<d>` | `POST /v1/fraud/predict` | `fraud-api.ml-platform:80` | 50/s |
| `a2a.<d>` | `/api/a2a/ewallet-agents/fraud-coordinator` and below | `kagent-controller.kagent:8083` | 5/s |
| `agents.<d>` | `/` | `kagent-ui.kagent:8080` | 100/s |
| `registry.<d>` | `/` | `prompt-registry-agentregistry.ewallet-prompts:12121` | 50/s |
| `grafana.<d>` | `/` | `grafana.ewallet-agents:3000` | 50/s |

Service names and ports were read from the cluster. Grafana serves rows 37 to 39, since logs and traces are read through it. Anything else answers 404.

**Not routed:** MCP servers and gateway, LLM agentgateway, vLLM, Redis, the KServe predictor, Vault, Jenkins, MLflow and the Kubernetes API.

## Security

| Control | How it works |
|---|---|
| HTTPS | TLS 1.2 or later on port 443; port 80 only redirects |
| Basic Auth | Envoy `basic_auth` filter, reading a mounted Secret |
| Rate limit | Envoy `local_ratelimit`, per route, HTTP 429, with a default for any route without its own |
| Order | Rate limit, then Basic Auth, then routing, so guessing passwords spends tokens |
| Identity headers | `authorization`, `x-user-id` and similar are removed before the upstream |
| A2A user | `x-user-id` is fixed at the gateway, because the kagent controller trusts it |
| Retries | Off: agent and prediction calls may not be safe to repeat |
| Failure | An unreadable password file rejects the listener, so the gateway fails closed |

**Limits to accept:**

- **Weak hash.** Envoy accepts only `{SHA}` htpasswd entries. The Secret is protected by RBAC, and the script demands 24 or more characters. Use a long random password.
- **One credential set.** All routes share it. Per-route users need a different design.
- **Per-replica limits.** The limit applies per gateway pod. It is exact at one replica.
- **Password change needs a restart.** Envoy reads the file at start-up.
- **`EnvoyFilter` is tied to Envoy internals.** Re-run the checks on every Istio upgrade.

## Local validation

`scripts/edge/local_validate.sh`, results in [`results.jsonl`](../../../docs/evidence/edai2/edge/local_real_istio_20261107T070724Z/results.jsonl). All 34 checks passed through real TLS and Envoy.

| Check | Result |
|---|---|
| No, wrong or forged credentials | 401 |
| Browser login prompt | `WWW-Authenticate: Basic` |
| Valid credentials | Accepted |
| Plain HTTP | 301 |
| Undeclared paths, wrong method | 404 |
| Other controller APIs and agents | 404 |
| Internal and unknown hosts | Not served |
| Password reaches the upstream | No |
| Client-chosen user id | Replaced |
| Burst of 60 | 55 × 429; other routes unaffected; route recovers |
| SSE | Events arrive 0.7 s apart, not buffered |
| Failed call | Upstream called once, no retry |

Real Envoy found two defects that template tests could not:

1. Istio prefixes do not stop at a path boundary, so `fraud-coordinator-other` was routed. Fixed with an exact match plus a slash-terminated prefix.
2. Basic Auth ran on port 80, asking for a password over plain HTTP. Filters now attach to port 443 only.

**Not tested locally:** the real kagent UI and Registry UI, cookies, WebSockets, ambient HBONE to upstreams, cert-manager issuance and a public load balancer. The negative test script runs unchanged on GKE without `--stub`.
