# G2.2 and G2.3: edge security and offline acceptance on kind (2026-11-21)

All runs are local, on `kind-llm-agent-local`: Kubernetes v1.37.0, Istio 1.30.7 with istiod, istio-cni and ztunnel, and Gateway API v1.7.0 Standard CRDs. Each run used the namespaces `ewallet-ingress`, `edge-stub` and `edge-intruder`, created by that run and deleted afterwards. GKE was not touched and stayed asleep.

**Real components:** istiod, ztunnel, kindnet NetworkPolicy, the official `istio/gateway` 1.30.7 chart, Envoy, the `ewallet-edge` chart, TLS, Basic Auth and local rate limiting.

**Stand-in:** one echo backend (`scripts/edge/stub/echo_stub.py`) behind all four routes. The real kagent UI, Agent Registry and Grafana are not deployed on kind; they belong to the frozen Agent Platform. This is local evidence, not GKE evidence.

## Commands

```bash
scripts/edge/local_validate.sh <dir> full         # G2.2: securityFiltersVerified=true
scripts/edge/local_validate.sh <dir> foundation   # default security state (filters not rendered)
uv run --locked --project infra/docker/llm-agent-tooling --group test python -m pytest -q \
  tests/llm_agent/test_c5_keda_chart.py tests/llm_agent/test_c7_chart.py tests/llm_agent/test_production_guard.py tests/ops/test_session_a_charts.py
uv run --locked --project infra/docker/llm-agent-tooling python -m unittest \
  tests.llm_agent.test_ci_scripts tests.llm_agent.test_gke_cpu_profile tests.llm_agent.test_vllm_runtime_chart
```

## Results

| File | Passed |
|---|---|
| `full/status.jsonl`: API status, ownership, Envoy config, TLS, network probes, restart, fail-closed | 26/26 |
| `full/results.jsonl`: HTTP checks through the gateway | 47/47 |
| `full/results-after-restart.jsonl`: the same checks after a gateway restart | 47/47 |
| `foundation/status.jsonl`, `foundation/results.jsonl` | 14/14, 31/31 |
| Component CI tests (`llm-serving`: pytest, unittest) | 78/78, 63/63 |
| `static/`: lint, guards, planned G3 render and server dry run on kind | 16/16 resources accepted |

`test-summary.json` holds the counts per file. `SHA256SUMS` covers every file in this directory.

## G2.2 acceptance

| ID | Requirement | Observed | Verdict |
|---|---|---|---|
| S01 | Missing credentials | 401 | PASS |
| S02 | Invalid credentials | 401 for a wrong password, an unknown user and a Bearer token | PASS |
| S03 | Valid credentials | 200 with the stub's echo body. A 5xx never counts | PASS |
| S04 | Browser challenge | `WWW-Authenticate: Basic realm="E-Wallet"` | PASS |
| S05 | EnvoyFilter attached | On the 443 HCM the chain runs `local_ratelimit`, then `basic_auth`, then `router`. The port 80 chain has neither filter | PASS |
| S06 | No authentication bypass | Forged `x-edge-user`, `x-forwarded-user` and `x-user-id` headers get 401. Unauthenticated unknown hosts and paths get 401 before routing. An unauthenticated burst got 0 × 200 | PASS |
| S07 | Rate limit | Registry bucket 50/s: a burst of 120 got 56 × 200 and 64 × 429, with no other status | PASS |
| S08 | Recovery | 200 two seconds after the burst | PASS |
| S09 | Route-level configuration | Envoy routes `feature-api`/`kagent-ui`/`agent-registry`/`grafana` carry token buckets 100/100/50/50, matching `values.yaml`. Envoy route names equal the HTTPRoute rule names | PASS |
| S10 | Four route definitions | Each host reaches its backend. Here every backend is the stub. Real UIs: see "Not tested locally" | PASS (definitions) |
| S11 | Feature API exact path and method | Only `POST /v1/features/lookup`. GET gets 404. `/metrics`, `/docs`, `/openapi.json`, `/health/ready` and `/internal/...` get 404 | PASS |
| S12 | ReferenceGrants | One per route, in the backend namespace, from `HTTPRoute` in `ewallet-ingress` to one named Service. ResolvedRefs True | PASS |
| S13 | NetworkPolicy and AuthorizationPolicy | See "Network security" | PASS |
| S14 | HTTPS locally | TLS 1.2 handshake succeeds and TLS 1.1 is refused (Envoy's default; Gateway API has no floor setting) | PASS |
| S15 | HTTP to HTTPS | 301 | PASS |
| S16 | Unknown hosts and paths | Unauthenticated: 401. Authenticated: 404 | PASS |
| S17 | No LoadBalancer | None before or after any run. The gateway Service is ClusterIP by default; NodePort is used only for this kind test | PASS |
| S18 | Frozen components unchanged | No diff under `infra/helm/llm-agent-platform`, `jenkins/`, `scripts/ci` or `ai_platform`. Helm releases on kind were unchanged | PASS |
| S19 | Real evidence | Machine-readable results from live requests and the Envoy config dump | PASS |
| S20 | No cloud writes | None | PASS |

### Network security (ambient, default-deny backend)

The stub namespace was configured like `ewallet-agents`: ambient mode, a default-deny NetworkPolicy, and the chart's ALLOW-only AuthorizationPolicy with principal `cluster.local/ns/ewallet-ingress/sa/ewallet-edge-gateway`.

- Before any policy, a control pod reached the stub (`REACHED`).
- Afterwards, three pods were blocked:
  - a pod in another namespace;
  - an unlabelled pod in `ewallet-ingress`: both refused by the NetworkPolicy;
  - a pod in `ewallet-ingress` carrying the gateway's own labels but the default ServiceAccount: it passes the NetworkPolicy and is reset by ztunnel (`ConnectionResetError`), so the AuthorizationPolicy is what stops it.
- The gateway reaches the stub over HBONE (`connect_originate` to pod port 15008).

### Fail closed

The EnvoyFilter was pointed at a missing users file.

- **Running proxy:** Envoy NACKed the listener (`Invalid path: /etc/edge-auth/missing`) and kept its last good one, so anonymous requests still got 401.
- **Restarted pod:** its 443 listener was rejected. The pod has no HTTPS listener and never becomes Ready, so the rolling update keeps the old pod. A broken auth configuration therefore never serves unauthenticated traffic.
- **Missing Secret** (reasoned, not run): the gateway pod cannot mount it and does not start.

## G2.3 offline acceptance

| ID | Scenario | Observed | Verdict |
|---|---|---|---|
| I01 | Gateway ownership | 1 Deployment and 1 Service from the `istio/gateway` release; `ownership_preflight.py live` OK | PASS |
| I02 | Gateway API conditions | GatewayClass Accepted. Gateway and both listeners Accepted and Programmed. 5/5 HTTPRoutes Accepted and ResolvedRefs | PASS |
| I03 | HTTPS | TLS 1.2 handshake; HTTPS requests succeed | PASS |
| I04 | Redirect | 301 | PASS |
| I05, I06 | Basic Auth | As S01 to S04 | PASS |
| I07, I08 | Rate limit and recovery | As S07 and S08, also after the restart | PASS |
| I09 | Host routing | Four hosts reach the backend | PASS (stub) |
| I10 to I13 | Allowlist, unknown host, unknown path, internal endpoints | As S11 and S16; hosts `a2a`, `fraud`, `vllm`, `mcp`, `redis`, `kserve`, `prometheus`, `jenkins`, `vault`, `mlflow` not served | PASS |
| I14 | ReferenceGrants | As S12 | PASS |
| I15 | Network security | Above | PASS |
| I16 | No retries | One backend attempt each for 200, 503, connection reset and 504 timeout. Envoy routes have no `retry_policy` | PASS |
| I17 | Streaming | SSE events at 0.05, 0.71, 1.41 and 2.11 s (not buffered); WebSocket upgrade 101 | PASS |
| I18 | Helm release safety | Releases on kind identical before and after | PASS |
| I19 | No public resources | No LoadBalancer, DNS record or certificate | PASS |
| I20 | Cleanup | Campaign namespaces, port-forwards and work directories removed | PASS |

### Regression against the 2026-11-07 baseline (34 checks)

| Baseline check | Now |
|---|---|
| "valid credentials pass authentication" (accepted any non-401 status) | Replaced by "valid credentials reach the application": a 200 with the echo body, or the Feature API's own 422 on GKE |
| Five "controller path … is not routed" checks on the `a2a` host | The A2A route was removed by owner decision. Covered by "host a2a is not served" |
| "a client cannot choose the agent user id" (relied on the A2A route pinning it) | Replaced by "a client-chosen user id is removed" |
| "the gateway does not retry a failed call" (503 only) | Replaced by four no-retry cases with a per-request counter |
| The other 26 | Unchanged and passing |

"host … is not served" now also sends valid credentials and requires 404 or no TLS. Without credentials a 401 is accepted, because the wildcard listener terminates TLS for any subdomain and authentication runs before routing.

## Not tested locally (G3/G4, on GKE)

- **Real UIs.** The kagent UI (root, static assets, its `/api` calls, chat streaming), the Agent Registry UI and Grafana run behind the edge only on GKE.
- **Grafana root URL.** Grafana has no `GF_SERVER_ROOT_URL`, so it uses the default `http://localhost:3000/`. Absolute redirects may point there. Setting it is a change to the frozen `llm-agent-platform` chart and needs owner approval.
- **Grafana login.** Grafana keeps its own login. The edge strips `Authorization`, so the two logins do not collide.
- **kagent controller API.** The kagent UI serves the controller's API through itself. Anyone holding the shared Basic Auth credential can use that API: list, create and delete agents. This follows from exposing the Agent Test UI (row 40).
- **Registry policy.** The NetworkPolicy targets pod port 8080, taken from the Jenkins policy that CI/CD verified, with no HBONE port. It assumes `ewallet-prompts` is not ambient (`PENDING_G3_PREFLIGHT`).
- **Per-pod rate limits.** Local rate limiting counts per gateway pod: with N replicas the effective limit is N × the bucket. The chart pins 1 replica.

## G3 handover (design only; nothing below was done)

Order:
1. Owner approves the wake.
2. Run `cloud_lab.py` wake with its recovery manifest and take a runtime baseline.
3. **Install Gateway API v1.7.0 Standard CRDs.** See the closure evidence `../g21_closure_20261121T130500Z/`. The fallback is v1.5.1 Experimental. The chart refuses v1.5.1 Standard.
4. Check that GatewayClass `istio` is Accepted.
5. Run `ownership_preflight.py live` (expect OK). Check that the live `llm-agent` release values have `gateway.enabled: false`, because `values-gcp.yaml` must never be used.
6. Create namespace `ewallet-ingress`.
7. Create the auth Secret (`create_basic_auth.sh`; password from a password manager, 24 characters or more).
8. Create the TLS Secret, or cert-manager `Certificate` (`tls.certificate.enabled`, issuer chosen by the owner).
9. Install the `istio/gateway` 1.30.7 release `ewallet-edge-gateway` with `values-gateway.yaml` (ClusterIP).
10. Install the `ewallet-edge` chart with the real domain, `securityFiltersVerified=true` and the four routes.
11. Run `negative_tests.py` without `--stub` through a port-forward.
12. Run `ownership_preflight.py live` again.
13. Owner decides the public LoadBalancer: `service.type=LoadBalancer`, then add `--allow-loadbalancer ewallet-ingress/ewallet-edge-gateway` to the preflight.
14. DNS records (owner).
15. External tests and evidence (G4).
16. Guarded sleep.

**Rollback:**
- `helm uninstall ewallet-edge -n ewallet-ingress`, which removes the Gateway, the HTTPRoutes, the EnvoyFilter, and the ReferenceGrants and policies in the backend namespaces. Then `helm uninstall ewallet-edge-gateway -n ewallet-ingress` and delete the namespace and DNS records.
- Remove the CRDs only if nothing else uses Gateway API.
- Backends need no change: every object the edge adds elsewhere is additive and labelled `app.kubernetes.io/part-of: ewallet-edge`.

**Cost (estimates from `SESSION_A_APPROVAL_PACKAGES.md`; confirm before use):**

| Item | Estimate |
|---|---|
| Nodes for the campaign | about $0.46/hour (2 nodes) |
| LoadBalancer forwarding rule and IP | about $0.03/hour |
| Cloud DNS zone, if used | billed monthly |
| ACME certificate | free |
