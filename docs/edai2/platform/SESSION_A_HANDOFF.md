# Session A handoff

Session A added the Feature API, real-backend Locust, serving and agent KEDA, and the Istio edge. All of it is validated locally. **Nothing is deployed.** GKE stayed at zero nodes, and no cloud rubric item is complete.

Branch: `feature/session-a-serving-gateway`, based on `9799ebd`. Local commits only.

## Audit result

| Area | Before | Now |
|---|---|---|
| Feature API | Missing on every branch | New Deployment sharing the Fraud API image and `FeastOnlineAdapter` |
| Load fixtures | One fixed timestamp, which most entities would fail | Per-entity fitted timestamp; freshness unchanged |
| Locust | Mock-backed on B1 | `feature` and `inference` targets beside it |
| Prometheus | Scraped through a Service | Per-pod discovery, opt-in |
| Serving KEDA | None | Feature and Fraud API, 1 to 3 |
| Agent KEDA | All three on an in-flight gauge | Drift Specialist moved to CPU: it exports no gauge and could never have scaled |
| KEDA scope | `ewallet-agents` only | Also `ml-platform` |
| Gateway | Template that could not work | `istio/gateway`, TLS, Basic Auth, rate limit |

Effective agent mode on GKE: kagent `Agent` with Deployments, three CPU HPAs and no KEDA installed. WorkerPool autoscaling applies to sandbox mode only, which production does not use.

## Autoscaling

**Local result, real KEDA 2.21, real Prometheus, real HTTP load, stand-in pod:**

| Step | Result |
|---|---|
| Load applied | 1 → 3 replicas in 36 s |
| Load removed | 3 → 1 in 117 s, with a test scale-down window of 20 s |
| Prometheus lost | Fallback engaged and held replicas |
| Drift Specialist object | Accepted by the real webhook |

Thresholds were lowered for that test and prove nothing about capacity. Production values start at 150 and 25 requests per second per replica, and must be set from the fixed arms.

With `minReplicaCount` 1, KEDA warns that `pollingInterval` and `cooldownPeriod` do not govern scaling: the generated HPA's behavior does.

The real webhook also refused `fallback` on a CPU-only object, so the Drift Specialist has none. The latency trigger uses `Value`, which KEDA accepted beside an `AverageValue` trigger.

**KServe (A-07): blocked, by design.** The predictor Deployment is owned by the InferenceService with `autoscalerClass: none`. Whether the controller rewrites `replicas` is unproven, and two owners would fight. No scaler exists. To unblock it, scale the predictor on a woken cluster and watch whether KServe reverts it.

## Rubric status

Row numbers come from `docs/rubric/edai_k11_workbook_extract.json`.

| Row | Requirement | Implemented | Local | Cloud | Missing |
|---|---|---|---|---|---|
| 16, 17 | FastAPI, validation, healthcheck, async | Feature API | Tests pass | Not run | P-A1 |
| 19, 22, 25 | Agents with multi-replica autoscale | KEDA triggers | Real KEDA, stand-in | Not run | P-A4 |
| 32 | Locust HTML on the Web API | Yes | Real local stack | Not run | P-A2 |
| 37 to 39 | Metrics, logs, traces behind gateway | Grafana route, off | Stub route | Not run | P-A5 |
| 40 | Agent UI behind gateway | Yes | Stub route | Not run | P-A5 |
| 41 | Registry UI behind gateway | Yes | Stub route | Not run | P-A5 |
| 42 | Basic Auth and rate limit | Yes | Real Envoy | Not run | P-A5 |
| 43 | Domain and HTTPS | Yes | Real TLS, local CA | Not run | Domain, P-A5 |
| 46 | Web API metrics | Yes | Series confirmed | Not run | P-A2 |

**Gap to review:** the rubric's Web API group mentions user data *and chunks*. The Feature API serves features only. Chunks are reachable through the MCP `chunk_lookup` tool, with no HTTP route yet.

The final coursework rubric sheet is not in this repository, so serving autoscaling is not mapped to a row number. The workbook extract holds only `edai-1` and `edai-2`.

## Compared with RecSys-MLops

| Area | Reference | E-Wallet | Why |
|---|---|---|---|
| Locust | One file, target by variable | Same idea, plus contract checks and manifests | Reused |
| Serving KEDA | Request rate and latency, 1 to 3 | Same triggers | Reused; thresholds not copied |
| Metrics | Pod scraping | Headless Service and DNS discovery | No Prometheus RBAC change |
| Agent KEDA | Not the main focus | In-flight gauge, CPU for Drift | Follows each runtime |
| Gateway | NGINX annotations | Istio with Envoy filters | Istio already runs here |
| UIs | Public HTTPS routes | Same, off by default | Owner approval first |

## Merge dependencies

Session A is based on C-C, which contains C-B. C-A and B1 are not included. `tests/load/__init__.py` is byte-identical to B1, and the new load files use new names.

## Open blockers

1. Billing and credit, as in the C-C handoff.
2. A domain and issuer for HTTPS.
3. New images: Fraud API, plus Coordinator and Specialist.
4. Real entity coverage is unknown until `prepare` runs on GKE.
5. KServe scaling ownership.
6. Real UIs behind the gateway are untested.
7. No HTTP route for chunks.
