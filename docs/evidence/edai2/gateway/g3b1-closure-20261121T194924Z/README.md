# G3-B1 closure: public-exposure preflight, DNS cleanup, repository-first scripts

Offline only. No change to GKE (0 nodes), Dynu, LoadBalancers or certificates. The only
outside calls were read-only DNS queries (`dns-now.jsonl`; values withheld) and read-only
kubectl reads of the sleeping cluster (`platform_checks.py sleep-feasibility`, `gateway`).
Base 7b7aea96. The replayable procedure is
[`docs/runbooks/edai2/EXTERNAL_GATEWAY_RUNBOOK.md`](../../../../runbooks/edai2/EXTERNAL_GATEWAY_RUNBOOK.md).

## A. Public-exposure preflight (L0)

- **Problem.** G3-B1's public mode only checked that an `edge-source-restriction` policy *existed*. A policy with the wrong selector, hostname, port or CIDRs, or with an extra condition that narrows the DENY, would have passed.
- **Change** (`scripts/edge/ownership_preflight.py`, public mode only).

  | Check | Rule |
  |---|---|
  | Selectors | the EnvoyFilter and the AuthorizationPolicy must select the pod labels of the actual `ewallet-ingress/ewallet-edge-gateway` Deployment (from the render or the cluster). No Deployment means "cannot prove", which fails |
  | EnvoyFilter | Basic Auth and the local rate limit must be `HTTP_FILTER` patches with `context: GATEWAY` on listener port 443 |
  | Port 80 | every application HTTPRoute is attached only to the `https` listener; `https-redirect` has no backend; `acme-http01` sends only the challenge prefix, on `http`, to the responder |
  | Source restriction | `edge-source-restriction` (namespace `ewallet-ingress`) is DENY, selector only, exactly one rule. The rule holds only `from`/`to`; the only source condition is `notIpBlocks`; the only operation conditions are `hosts` ⊇ {`agents.<domain>`, `agents.<domain>:*`} and `ports == ["443"]`. The agents route must serve exactly `agents.<domain>` |
  | CIDRs | parsed with `ipaddress.ip_network`: IPv4 only, a prefix required, no host bits (the meaning must not change on normalisation), /16 or narrower, no overlap with 0/8, 10/8, 100.64/10, 127/8, 169.254/16, 172.16/12, 192.168/16 or 224/3. The set must **equal** the owner-approved list |
  | Approved list | from `--approved-cidr` or `--approved-cidrs-file`; a file inside the repository is refused. No list means the agents route cannot pass |
  | Domain | `--domain` is required in public mode |
  | Live | additionally, every gateway pod's active 443 chain has local_ratelimit, rbac and basic_auth, and the RBAC filter holds a DENY policy from `edge-source-restriction` whose remote-address prefixes equal the approved list. This is configuration evidence only; L4/L5 black-box tests stay mandatory |

  The Helm template now also rejects octets above 255 (`999.999.999.999/32`).
- **Validation.** `tests/ops/test_public_preflight.py` uses real `helm template` renders and documentation addresses, mutating one field per case:

  | ID | Case | Result |
  |---|---|---|
  | PS-01 | valid configuration | PASS |
  | PS-02/03 | Basic Auth or rate limit missing; filter on port 80 | FAIL |
  | PS-04 | restriction missing | FAIL |
  | PS-05 | wrong selector (policy or EnvoyFilter); no gateway Deployment | FAIL |
  | PS-06 | old domain | FAIL |
  | PS-07 | ports none, `80`, `8443`, `443`+`80` | FAIL |
  | PS-08 | empty list | FAIL |
  | PS-09 | unapproved public CIDR | FAIL |
  | PS-10 | `999.999.999.999/32`, `/33`, no prefix, IPv6, host bits, garbage | FAIL |
  | PS-11/12 | private, shared, link-local, loopback, `/8`, `0.0.0.0/0` | FAIL |
  | PS-13 | agents disabled | PASS |
  | PS-15 | owner CIDR matches; no approval given | PASS; FAIL |
  | PS-16 | different live CIDR; narrowing `paths`/`when` | FAIL |
  | PS-17 | `externalTrafficPolicy: Cluster` | FAIL |
  | PS-18 | unapproved LB | FAIL |
  | PS-19 | ACME route to another backend; application route on port 80 | FAIL |
  | PS-20 | valid with the ACME responder | PASS |
  | live | RBAC dump matches, differs, ALLOW instead of DENY, Basic Auth missing | as expected |
  | CLI | end-to-end `render` with and without `--domain`/approved CIDRs | as expected |

  41 tests, LOCAL (unit). PS-14 (forged `X-Forwarded-For`) is KA-04 in the kind run: LOCAL_KIND.

## B. Dynu DNS cleanup

- **Problem.** The plan restored the "previous" Dynu address, which was the owner's home network.
- **Change.** The runbook's DNS cleanup:
  1. take the hostname offline in Dynu (Offline Settings, or remove the IPv4), never back to the home address;
  2. verify with `dns_check.py --expect-offline` at all authoritative servers and two public resolvers, for the 4 names, the bare name and a random wildcard name;
  3. **only then** release the LB.

  A placeholder offline IP, if Dynu insists on one, is an owner decision (`--offline-ip`).
- **State today** (`dns-now.jsonl`, read-only):

  ```text
  DNS hostname:              ewallet-hg.gleeze.com
  Wildcard A:                present at every server (value withheld; not an LB)
  Wildcard AAAA:             none
  Current target:            withheld (the address Dynu took at registration)
  Planned offline state:     Dynu Offline Settings / IPv4 removed (owner)
  Authoritative DNS result:  ns0/ns1/ns3/ns4/ns5/ns6.dynu.com: A present, AAAA none (48 of 48 lines "not offline")
  Public resolver result:    1.1.1.1, 8.8.8.8: same
  LB released:               no LB exists
  Final DNS state:           DNS_OFFLINE_NOT_YET (the names still resolve to the registration address)
  ```

## C. Repository-first automation (owner requirement)

Every reusable step now lives in `scripts/edge/`, with focused tests in `tests/ops/test_edge_scripts.py` (47) and the runbook:

| Was ad hoc | Now |
|---|---|
| G3-A1 backend baseline/regression, Jenkins→Registry, Grafana→sources | `platform_checks.py backends / compare` |
| kagent-ui readiness watch after the policy | `platform_checks.py watch` |
| Gateway API conditions, Envoy config (filter order, buckets, retries, TLS secret), first config of the staged rollout | `platform_checks.py gateway / envoy [--wait] [--restricted]` |
| G3-A1 pre-wake PDB prediction | `platform_checks.py sleep-feasibility`. Read-only run against the sleeping cluster reproduces the G3-A1 gate: `ewallet-mcp/feature-rag-mcp` not covered |
| Real-backend E2E (G3-A1) and the G4 rows | `public_e2e.py` (rows 37–41, 43; evidence class required). Row 42 stays `negative_tests.py --rate-limit-mode http2` |
| Grafana root-URL check in Docker (G3-B0) | `grafana_root_url_check.sh` (pinned image; integration test ran) |
| Staged 0 → 1 rollout check (was only in an evidence folder) | `staged_rollout_check.sh` (kind only; the evidence copy stays as history) |
| Certificate issuance and import; temporary self-signed certificate | `issue_certificate.sh` (staging default, production needs `--confirm-production`, private dir outside the repo; the kind ACME phase now runs this script) |
| DNS verification | `dns_check.py` (values that are not the expected LB address are withheld) |
| Frozen-chart (Grafana) upgrade gate | `release_diff.py` (no drift against live; only `GF_SERVER_ROOT_URL` on Grafana) |

Not converted, because it is not a reusable acceptance step: the one-off mutation proof from
G2 hardening (old assertions restored in a scratch copy).

## Tests

| Check | Result |
|---|---|
| compile, `bash -n` (12 scripts), helm lint | OK |
| G3-A1 edge render | byte-identical |
| llm-agent chart | unchanged since 7b7aea96 |
| edge chart | 1 line changed (octet regex) |
| `tests/ops` | 344/344 (public preflight 41, edge scripts 47) |
| kind full with ACME | `kind/full/`, see the final report |
| NOT_RUN | everything live: GKE, Internet, Dynu, Let's Encrypt (`PENDING_G3B_LIVE_VALIDATION`) |
