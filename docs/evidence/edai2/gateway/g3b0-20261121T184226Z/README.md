# G3-B0: public readiness and security hardening (offline)

Offline only. No GKE mutation: GKE stayed at 0 nodes. The only GKE calls were read-only
lookups (issuers, DNS zones, static IPs) in `tls-dns-readonly.txt`. Runtime results come from
local kind (LOCAL_KIND) and local Docker (LOCAL_DOCKER), never from GKE. Base: d20da8d1.

| Package | Status |
|---|---|
| A. kagent UI access | Fail-closed source restriction implemented and verified on kind. GKE source-IP preservation is `PENDING_G3B_LIVE_VALIDATION` |
| B. Grafana root URL | Opt-in value implemented; Grafana-only diff proven; pinned image verified locally |
| C. HTTP/2 rate-limit test | Standardised with a PASS/FAIL/INCONCLUSIVE verdict; verified on kind in both modes |
| D. Domain, DNS, TLS, LB | Plan and overlay ready. Domain `PENDING_OWNER_DOMAIN` ([G3B_DEPLOYMENT_PLAN.md](G3B_DEPLOYMENT_PLAN.md)) |
| E. Evidence | [G3B_G4_EVIDENCE_PLAN.md](G3B_G4_EVIDENCE_PLAN.md) |

## A. kagent UI access (P0)

- **Problem.** `agents.<domain>` routes `/` to the kagent UI, which proxies the controller API. The controller runs `AUTH_MODE=unsecure`, so anyone holding the shared Basic Auth credential could create or delete agents. Basic Auth is authentication, not authorisation.
- **Change.** Two independent layers, both off by default and both fail-closed:
  1. **Network (Option A):** `values-gateway-public.yaml` for the existing istio/gateway release. It sets `type: LoadBalancer`, `externalTrafficPolicy: Local` and **required** `loadBalancerSourceRanges`, which GKE enforces as a VPC firewall rule before any proxy. Only ports 80 and 443 are exposed. `ownership_preflight.py` (render and live) refuses an approved edge LoadBalancer without source ranges, with `/0`, or without `Local`.
  2. **Envoy (Option B) for the agents host:** `ewallet-edge` `sourceRestriction` renders a DENY `AuthorizationPolicy` on the gateway pod. It refuses `agents.<domain>` and `agents.<domain>:*` unless the **TCP peer address** (`ipBlocks`, never `X-Forwarded-For`) is in `allowedCidrs`. An empty list, a `/0`, a non-IPv4 value or a disabled route fails the render. Other routes are untouched.

  Option C (keep agents private) remains the fallback: leave `routes.kagent-ui.enabled=false` in the public overlay.
- **Why not more.** No Keycloak, OAuth2 proxy or authorisation service, as the brief requires. Access is limited to the owner and graders, from approved addresses, for the demo window.
- **Validation (kind, 15 checks in `kind/full/status.jsonl`).** Envoy saw peer 10.244.0.1 (kind SNATs NodePort traffic).

  | ID | Check | Observed |
  |---|---|---|
  | KA-01 | source outside the allowlist | valid credentials 403; no credentials 403 (RBAC runs before Basic Auth) |
  | KA-02/03/05 | source in the allowlist | valid credentials served; none 401; wrong password 401 |
  | KA-04 | forged `X-Forwarded-For`, `X-Real-IP`, `X-Envoy-External-Address` set to an allowed address | 403 |
  | KA-06 | `/api/agents`; Host without port, uppercase with port, trailing dot | 403, 403, 403, 404 |
  | KA-07 | unknown host (main harness) | anonymous 401, authenticated 404 |
  | KA-08 | restriction removed | G3-A1 behaviour |
  | KA-09 | empty, `/0`, invalid allowlist | render fails (chart tests) |
  | KA-10 | features, registry, observe with the restriction on | all served |

- **Remaining risk.**
  - On GKE the policy is only meaningful if Envoy sees the real client address. That needs `externalTrafficPolicy: Local` and is `PENDING_G3B_LIVE_VALIDATION` (live gate L5 in the plan).
  - istiod warns that a DENY rule with only HTTP attributes also denies TCP traffic in scope. The gateway serves only HTTP(S), so this is fail-closed and harmless. A port match was not added because a mismatched port would fail open.

## B. Grafana root URL (P1)

- **Problem.** On G3-A1, Grafana reported `appUrl: http://localhost:3000/`, so absolute links would point to localhost.
- **Root cause.** `GF_SERVER_ROOT_URL` is not set in `llm-agent-platform/templates/observability-stack.yaml`.
- **Change.** A top-level value `grafanaExternal.rootUrl` (default `""`). When set, Grafana gets `GF_SERVER_ROOT_URL`; it must be `https://<host>/`, otherwise the render fails. It is kept **outside** `observabilityStack`, because `checksum/observability-config` hashes that whole map and would restart Prometheus.
- **Validation.**
  - All 8 source-defined production profiles: `PRODUCTION_RENDER_BYTE_IDENTICAL` against d20da8d1 (`grafana/render-equivalence.txt`).
  - With `observabilityStack.enabled=true` and the value empty: byte-identical (69 objects).
  - With the value set: one object changes (the Grafana Deployment, plus one env line, `grafana/grafana-render.diff`). Annotations are unchanged, and Prometheus, Loki and Tempo are identical.
  - Pinned image `grafana:12.3.0@sha256:70d9…` in local Docker: default `appUrl` `http://localhost:3000/`; set `https://observe.edge.g3a.test/`. Redirects stay relative, static assets 200 (`grafana/grafana-local-runtime.txt`).
- **Rollback.** Unset the value and helm-upgrade `llm-agent`; only Grafana restarts.
- **Remaining risk.** This is a frozen-chart change. It needs its own owner approval of the exact diff at G3-B.

## C. HTTP/2 rate-limit test (P2)

- **Problem.** In G3-A1 the thread burst through port-forward reached about 62 req/s and saw no 429. The harness reported FAIL, but the run proved nothing either way.
- **Change.**
  - `negative_tests.py --rate-limit-mode http2` reuses the G3-A1 method: one curl, `--http2 --parallel`, credentials on stdin, one global `-w` with `%{filename_effective}` so each line maps to its request.
  - `rate_limit_verdict()` decides from observed counts only:

    | Verdict | Condition |
    |---|---|
    | PASS | real answers and 429s, nothing else |
    | FAIL | unexpected statuses (5xx, 401/403 with valid credentials, wrong redirect), or more requests than one pod's bucket can hold during the burst and no 429 |
    | INCONCLUSIVE | connection failures, nothing served, or too few requests to exceed the bucket |

  - The bucket is per gateway pod. Thresholds, Basic Auth and the burst size (120, registry route only) are unchanged.
- **Validation.**
  - Unit tests: G3-A1 shapes (58/62 PASS; 120 × 200 in 1.94 s INCONCLUSIVE), throughput too low, connection failure, 500, 401, recovery and other-route failure, and HTTP/2 parsing against a fake curl that behaves like real curl.
  - Kind (after restart, http2): `PASS total=120 served=51 429=69`; recovery 200; other route 200. Thread mode: `PASS served=55 429=65`.
  - Attempt 1 (`kind/attempt1-http2-parser-bug/`) exposed my first parser, which used a per-URL `-w`; curl treats `-w` as global. The verdict correctly refused to call that run PASS.
- **Remaining risk.** None known offline. Live throughput through port-forward or a public LB is measured, not assumed.

## Tests

| Check | Result |
|---|---|
| compile, `bash -n`, helm lint (edge default, edge restricted, llm-agent) | OK (`tests/static.txt`) |
| G3-A1 edge render with the new chart | byte-identical |
| `tests/ops` | 242/242 |
| component CI (pytest, unittest) | 156/156, 63/63 |
| llm-agent chart and observability tests | 144 passed, 2 skipped |
| kind foundation | status 14/14, HTTP 32/32 |
| kind full | status 42/42 (27 + 15 KA), HTTP 48/48 (threads) and 48/48 after restart (http2) |
| NOT_RUN | Calico/GKE behaviour, public LB, real certificate, live source IP (G3-B live gates) |
