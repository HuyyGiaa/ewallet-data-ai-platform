# G3-B1: HTTP-01 and public gateway preparation (offline)

No GKE, DNS or ACME production action. GKE is at 0 nodes. Runtime results are LOCAL_KIND:
real Istio 1.30.7 gateway, Pebble 2.7.0 (Let's Encrypt's test CA) and certbot 5.10.0. Base
c33d1ff5. The G3-B plan is [G3B_DEPLOYMENT_PLAN.md](G3B_DEPLOYMENT_PLAN.md); the G4 evidence
list is unchanged in `../g3b0-20261121T184226Z/G3B_G4_EVIDENCE_PLAN.md`.

## A. HTTP-01 feasibility

- **Problem.** DNS-01 needs a TXT record; the new Dynu account cannot create one for 30 days.
- **Facts.**
  - The Dynu wildcard works: `ewallet-hg.gleeze.com` and `agents.…` resolve to the same address, TTL 119 s, NS `ns{1,3,4}.dynu.com`.
  - cert-manager 1.16.2 runs without Gateway API support, and there is no ClusterIssuer (G3-B0 read-only check).
  - The gateway's HTTP listener has only the `https-redirect` route (`/`, same namespace).
- **Decision.** certbot `--manual` with a repository hook that puts each token behind the existing gateway. One certificate with the four SANs.
  - Rejected: the cert-manager solver, which would change the shared cert-manager for a one-off certificate.
  - Rejected: certbot standalone or webroot on the laptop, because Let's Encrypt connects to the LB, not the laptop.
- **Result.** Feasible: issuance worked end to end through the real gateway on kind (ACME-08). On GKE it needs the open LB mode during issuance, because Let's Encrypt validates from unlisted networks.

## B. ACME challenge routing

- **Change.** `ewallet-edge` `acmeHttp01` (off by default) adds three objects:
  - **ConfigMap + Deployment:** a responder pod (python 3.12 alpine pinned by digest, non-root, read-only root, no service-account token, no env). It answers `/.well-known/acme-challenge/<[A-Za-z0-9_-]{1,128}>` from a file and 404s anything else.
  - **Service:** for that pod.
  - **HTTPRoute `acme-http01`:** on the `http` listener only, for the 4 hostnames, `PathPrefix /.well-known/acme-challenge/`.

  `scripts/edge/acme_http01_hook.sh` writes the token through `kubectl exec` stdin. Its `auth` step returns only after the token is served **through the gateway at the real hostname**; `cleanup` deletes it.
- **Validation (kind, `kind/full/status.jsonl`).**

  | ID | Check | Observed |
  |---|---|---|
  | ACME-01 | unknown token | 404 from the responder |
  | ACME-02 | other port-80 paths, root | 301 |
  | ACME-03 | `/.well-known/acme-challenge-x`; `../..` (normalised by Envoy); `%2F..` | 301; 301; 404 |
  | ACME-04 | challenge path on 443 without credentials | 401 |
  | ACME-05 | unrouted host | 404 |
  | ACME-06 | written token | 200 with the value |
  | ACME-07 | agents restriction on | agents challenge on 80 still 200; HTTPS agents 403 |
  | ACME-08 | Pebble issuance for features, agents, registry, observe through the gateway | certbot exit 0, 4 SANs |
  | ACME-09 | challenge files after issuance | 0 |
  | ACME-10 | imported certificate | verifies against the issuing CA; Basic Auth still 401 |
  | ACME-11 | SAN the gateway does not route | the hook detects it, certbot fails, cleanup ran, `edge-tls` unchanged |
  | ACME-12 | rollback | responder objects gone, challenge path 301 again, original certificate restored |
- **Remaining risk.** Let's Encrypt production behaviour (multi-perspective validation, rate limits) is `PENDING_G3B_LIVE_VALIDATION`; the plan rehearses it on Let's Encrypt staging first.

## C. Public access mode

- **Problem.** The owner wants an Internet-reachable LB, while G3-B0's preflight refused `0.0.0.0/0`.
- **Change.**
  - `values-gateway-public-open.yaml`, a separate file so the choice is explicit, sets `0.0.0.0/0`.
  - `ownership_preflight.py --allow-public-loadbalancer` accepts it only when the same render (or the live cluster) shows:
    - Basic Auth and the rate limit in `edge-security`;
    - the agents route behind the DENY `edge-source-restriction` (or not routed);
    - the ACME route pointing at the responder only.

    Live, it also reads every gateway pod's active 443 chain (local_ratelimit, rbac, basic_auth). `externalTrafficPolicy: Local` and ports exactly 80/443 stay required.
  - `sourceRestriction` is now scoped to the HTTPS port, so the agents challenge is reachable on 80. It also refuses private or shared ranges and anything wider than /16: a SNAT'd node or pod address can never be allowlisted, so if the client address were not preserved, agents would stay closed. Only the local kind test sets `allowPrivateCidrs`.
- **Validation.**
  - Preflight unit and render tests: open LB without the flag refused; with the flag, secure render OK; unrestricted agents refused; agents disabled OK; no EnvoyFilter refused; `Cluster` traffic policy refused.
  - Kind: KA-01..10 still PASS with the port-scoped policy.
- **Remaining risk.** Client-address preservation on GKE is `PENDING_G3B_LIVE_VALIDATION` (plan step 8, gate L5).

## D. Unchanged from G3-B0

Grafana `grafanaExternal.rootUrl` (target `https://observe.ewallet-hg.gleeze.com/`) and the HTTP/2 rate-limit verdict. No code change; both are re-run in the kind regression.

## Tests

| Check | Result |
|---|---|
| compile, `bash -n` (incl. hook), helm lint | OK |
| G3-A1 edge render | byte-identical (all new features off by default) |
| llm-agent chart | unchanged since c33d1ff5 |
| `tests/ops` | 255/255 |
| component CI | 169/169, 63/63 |
| kind full (`EDGE_ACME_TEST=1`) | status 61/61 (27 + KA 15 + ACME 19), HTTP 48/48 (threads), 48/48 after restart (http2 PASS) |
| NOT_RUN / PENDING_G3B_LIVE_VALIDATION | Let's Encrypt staging and production, Dynu update, public LB, GKE source-IP preservation, Calico, Internet E2E |
