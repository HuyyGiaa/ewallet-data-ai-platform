# G3-A1 private external gateway on GKE: g3a1-20261121T175830Z

Status: **G3A1_PRIVATE_VERIFIED_AND_SLEEP_COMPLETE**. Evidence class: GKE_PRIVATE (runtime on
`ewallet-dev`). Source 94f28422 (chart, scripts), plus evidence commits. No public
LoadBalancer, DNS or certificate. G3-B not started. The first pre-wake stop is in
`00-preflight/pre-wake-gate.md`.

## Timeline (UTC, 2026-11-21)

| Time | Step |
|---|---|
| 17:58 | Step 0 preflight. Stopped before wake: the feature-rag-mcp PDB was not covered (commit ffaca35e) |
| ~18:00 | Owner approved `--also-approve ewallet-mcp/feature-rag-mcp` for this campaign |
| 18:02:45 | Gateway API v1.7.0 Standard (sha256 4e2602c4…) server-side applied while asleep: 10 CRDs Established, VAP and binding `safe-upgrades` |
| 18:03–18:04:39 | `cloud_lab wake-full --nodes 5`: 5 × e2-standard-4 Ready, KEDA resumed |
| 18:09 | Settled. istiod created GatewayClasses istio, istio-remote, istio-waypoint (Accepted) |
| 18:10–18:11 | Backend baseline (direct): all four READY; Jenkins→Registry 200; Grafana→Prometheus/Loki/Tempo ready |
| 18:11:53 | Namespace `ewallet-ingress`; Secrets `edge-tls` (1-day ECDSA, SAN `*.edge.g3a.test`) and `edge-basic-auth` |
| 18:12:08 | `ewallet-edge-gateway` (istio/gateway 1.30.7, sha256 f8b350a2…) at replicaCount 0, ClusterIP |
| 18:13:14 | `ewallet-edge` installed: render identical to the approved closure render, 16 objects, no conflicts, server dry-run OK |
| 18:13–18:16 | kagent-ui watched 3 min under the new policy: Ready, restarts unchanged, no probe events |
| 18:17:40 | Gateway scaled to 1. First 443 config: local_ratelimit → basic_auth → router (SECURITY_GATE PASS) |
| 18:18–18:22 | Security tests, real-backend E2E, controlled restart, regression |
| 18:23:37 | Fresh recovery manifest (83 workloads incl. the edge gateway) |
| 18:24:07–18:28:52 | Guarded sleep: KEDA pause; fraud-coordinator, drift-mcp, feature-rag-mcp 2→0; resize to 0 |
| 18:29 | `verify --expect sleep`: failures [] |

## Results

| Group | ID | Result | Observed |
|---|---|---|---|
| Infra | INF-01 | PASS | kubeconfig server = cluster DNS endpoint; `~/.kube/config` unchanged |
| | INF-02 | PASS | 10 CRDs Established, served/stored versions as published |
| | INF-03 | PASS | GatewayClass istio Accepted; Gateway Programmed, bound to the Helm Service address |
| | INF-04/05 | PASS | 5 CPU nodes max, no GPU pool |
| | INF-06/07/08 | PASS | ClusterIP; ownership preflight live EDGE_OWNERSHIP_OK; 0 Istio-provisioned Deployments; 0 LoadBalancers |
| | INF-09/10 | PASS | ewallet-edge r1 and ewallet-edge-gateway r2 deployed; every backend-namespace object owned by release ewallet-edge |
| Security | SEC-01 | PASS | Verify return code 0 with the test CA; system trust fails (curl 60); TLS 1.1 refused, 1.2 and 1.3 accepted |
| | SEC-02/03 | PASS | missing, wrong, unknown, Bearer and forged-header requests → 401 with `WWW-Authenticate: Basic realm="E-Wallet"`; valid credentials → application answer |
| | SEC-04 | PASS | HTTP/2 burst on registry (198 req/s): 58 × 200, 62 × 429, nothing else. The harness's 30-thread burst reached only 62 req/s through port-forward and saw no 429; that check is recorded as FAIL in `06-security/results.jsonl` (client-rate artifact) |
| | SEC-05 | PASS | registry 200 after 2.5 s; the feature route answered 422 right after the burst |
| | SEC-06/10 | PASS | 11 internal/unknown hosts: anonymous 401, authenticated 404; undeclared Feature API paths and GET → 404; HTTP → 301 to HTTPS on the same host |
| | SEC-07 | PASS | 4 ReferenceGrants, each HTTPRoute@ewallet-ingress → one Service |
| | SEC-08 | PASS | kagent-ui policy: gateway on 8080+15008, 169.254.7.127/32 on 8080 only; the other policies are gateway-only |
| | SEC-09 | PASS | Grafana ALLOW for principal `cluster.local/ns/ewallet-ingress/sa/ewallet-edge-gateway`; default-deny policies intact |
| | SEC-11 | PASS | no Envoy retry_policy on any route |
| | SEC-12 | PASS | after a controlled restart: same filter chain, anonymous 401, valid credentials 422 |
| Backends | RB-01 | PASS | APPLICATION_REACHED: invalid body 422 `invalid_request` with correlation_id; synthetic lookup 424 `missing_feature`. BUSINESS_LOOKUP_SUCCESS not claimed (no synthetic data in the online store, and none created) |
| | RB-02 | PASS | UI 200 (HTML, static JS), `/health` 200, read-only `/api/agents` lists 4 agents. Chat streaming (SSE) not exercised: it would run an agent |
| | RB-03 | PASS | UI 200 (HTML, CSS), `/v0/agents` and `/v0/health` 200, no redirect |
| | RB-04 | PASS, with finding | `/` 302 `location: /login` (relative), login 200, 9 dashboards; Loki, Prometheus and Tempo datasources healthy. The frontend `appUrl` is `http://localhost:3000/` (no `GF_SERVER_ROOT_URL`), so absolute links would point to localhost: G3A_GRAFANA_ROOT_URL_DEPENDENCY |
| | RB-05 | PASS | Jenkins → registry:12121 200 before and after the edge |
| | RB-06 | PASS | Grafana pod → Prometheus/Loki/Tempo ready before and after |
| | RB-07 | PASS | readiness and restart counts identical to the pre-edge baseline (the 4–5 restarts on kagent/registry predate the edge: cold start); 158 Running, istiod, ztunnel 5/5, KEDA, WorkerPool 2/2, Valkey 6/6 |
| Ops | OPS-01 | PASS | `11-sleep/recovery-manifest` (the next wake restores the edge gateway too) |
| | OPS-02 | PASS | awake 0.43 h ≈ $0.43 compute (+$0.04 cluster fee before free tier); list-price estimate |
| | OPS-03/04 | PASS | this directory; credentials, keys, cookies and config dumps kept in the private local directory only |
| | OPS-05/06/07 | PASS | private edge retained; guarded sleep with the approved exception; 0 nodes, verify failures [] |
| | OPS-08 | PASS | mutations limited to the approved list |

## Cloud mutations (all approved)

- Gateway API CRDs plus the VAP and its binding.
- `wake-full` 0→5 nodes: labels on 3 nodes, KEDA resume, guarded Deployments restored.
- Namespace `ewallet-ingress` and 2 Secrets.
- 2 Helm releases: the gateway (r1 at 0 replicas, r2 at 1) and edge r1.
- One rollout restart of the edge gateway.
- Guarded sleep: KEDA pause of fraud-coordinator and business-agents-workerpool, 3 Deployments 2→0, resize 5→0.

## Remaining for G3-B (owner decisions)

- Public exposure: LoadBalancer, DNS and a real certificate (row 43).
- kagent-controller `AUTH_MODE=unsecure`: the shared Basic Auth credential reaches the controller API through the UI.
- Grafana `GF_SERVER_ROOT_URL` (a frozen-chart change) for correct absolute links.
- Rate-limit evidence for G4: the HTTP/2 client used here, or an approved in-cluster client.
- Screenshots: none were captured in this run. To capture them later from the laptop:
  1. Wake the cluster.
  2. Port-forward `svc/ewallet-edge-gateway 18443:443` on 127.0.0.1.
  3. Map the four `*.edge.g3a.test` names to 127.0.0.1 in a browser profile.
  4. Trust `06-security/edge-test-ca.crt`.
  5. Sleep the cluster again.
