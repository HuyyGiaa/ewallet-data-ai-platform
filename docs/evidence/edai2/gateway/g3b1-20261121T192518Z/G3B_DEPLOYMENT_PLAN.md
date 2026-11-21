# G3-B public deployment plan, HTTP-01 version (supersedes the G3-B0 plan)

Nothing here has run. Every step marked **[live]** is a GKE, DNS or ACME action that needs
the owner's approval of this plan. Hostnames: `features|agents|registry|observe.ewallet-hg.gleeze.com`
(Dynu, wildcard alias on). Certificate: one Let's Encrypt certificate with those four SANs
(no wildcard: DNS-01 is unavailable because Dynu allows no TXT records for 30 days).

## Why this order

HTTP-01 needs port 80 of every name reachable **from anywhere** (Let's Encrypt validates from
several networks) before a trusted certificate exists. So the load balancer is opened while
HTTPS still serves the temporary self-signed certificate. That is safe only because
everything an open LB relies on is already active on the same Envoy pod and verified first:
Basic Auth and the rate limit on 443, the agents host behind the source restriction on 443,
and port 80 limited to the redirect and the ACME token. The Service type change does not
restart the pod.

## Steps

| # | Step | Gate before the next step |
|---|---|---|
| 1 | **[live]** `cloud_lab.py wake-full --manifest ~/ewallet-evidence/gateway/g3a1-20261121T175830Z/11-sleep/recovery-manifest --nodes 5 --execute --confirm-cluster ewallet-dev` | nodes Ready, platform settled (G3-A1 checks) |
| 2 | Private check over port-forward: config dump (rate limit → Basic Auth → router on 443), 401/200, kagent-ui Ready | as in G3-A1 |
| 3 | **[live]** `helm upgrade ewallet-edge … -f g3a-private-values.yaml --set domain=ewallet-hg.gleeze.com --set sourceRestriction.enabled=true --set-json 'sourceRestriction.allowedCidrs=["<owner public IP>/32", …]' --set acmeHttp01.enabled=true`. Still ClusterIP | Gateway, HTTPRoutes and `acme-http01` Accepted/ResolvedRefs; responder Ready |
| 4 | Verify the active config: `ownership_preflight.py live --context … --allow-public-loadbalancer`. Before the LB exists it checks the edge objects and every gateway pod's 443 chain (local_ratelimit, rbac, basic_auth). Over port-forward: agents → 403 (the peer is not allowed); `http://…/.well-known/acme-challenge/x` → 404; other port-80 paths → 301 | `EDGE_OWNERSHIP_OK` and the three observations |
| 5 | Render check: `ownership_preflight.py render --allow-public-loadbalancer <gateway render with values-gateway-public.yaml + values-gateway-public-open.yaml> <edge render>` | `EDGE_OWNERSHIP_OK` |
| 6 | **[live]** `helm upgrade ewallet-edge-gateway <chart> -f values-gateway.yaml -f values-gateway-public.yaml -f values-gateway-public-open.yaml`. One LB, ports 80/443, `externalTrafficPolicy: Local` | EXTERNAL-IP assigned; `ownership_preflight.py live --allow-public-loadbalancer` OK |
| 7 | **[live, owner]** Dynu: set the IPv4 of `ewallet-hg.gleeze.com` to the LB IP. The wildcard alias covers the four names. Note the previous value first | `dig @ns1.dynu.com +short features.ewallet-hg.gleeze.com` = LB IP, also from a public resolver |
| 8 | Live security gates over the Internet, before any certificate: L2 (401 + challenge, valid → app), L4 (agents from a non-allowed network → 403), L5 (agents from the allowed IP → app, which proves the client address is preserved), L6 (internal hosts and paths not routed) | all PASS, else step 13 |
| 9 | **[live]** Staging issuance: `certbot certonly --server https://acme-staging-v02.api.letsencrypt.org/directory --manual --preferred-challenges http --manual-auth-hook "scripts/edge/acme_http01_hook.sh auth" --manual-cleanup-hook "scripts/edge/acme_http01_hook.sh cleanup" --key-type ecdsa --cert-name edge-staging -d features.… -d agents.… -d registry.… -d observe.…` with `EDGE_ACME_CONTEXT`/`EDGE_ACME_KUBECONFIG` set and config/work/logs dirs in the private evidence directory | staging certificate issued with the 4 SANs; no challenge file left |
| 10 | **[live]** Production issuance: the same command without `--server` and `--cert-name edge`, then `kubectl create secret tls edge-tls --cert fullchain.pem --key privkey.pem --dry-run=client -o yaml \| kubectl apply -f -`. Istio reloads it by SDS; no restart | L1: `openssl s_client -connect features.…:443 -servername features.…` verifies with system trust; SAN = 4 names; TLS 1.1 refused |
| 11 | **[live]** Responder off: `helm upgrade ewallet-edge …` without `acmeHttp01.enabled`. Then, if approved separately: `helm upgrade llm-agent … --set grafanaExternal.rootUrl=https://observe.ewallet-hg.gleeze.com/` (one env line; only Grafana restarts) | challenge path → 301 again; Grafana `appUrl` correct (L9) |
| 12 | Public E2E and G4: L3 (`negative_tests.py --address <LB IP> --domain ewallet-hg.gleeze.com --rate-limit-mode http2` → PASS), L7, L8, then the G3B_G4 evidence plan (G3-B0 campaign) | all PASS |
| 13 | **[live]** Close: `helm upgrade ewallet-edge-gateway … -f values-gateway.yaml` (ClusterIP; GKE removes the LB). Verify no LoadBalancer. Dynu IPv4 back to its previous value. Keep `edge-tls` (valid 90 days) and a private copy of the certificate and key | no LB; DNS no longer points to the released IP |
| 14 | **[live]** Fresh `record-manifest`; `plan-sleep`; guarded sleep with the approvals and only the PDB exceptions `plan-sleep` actually lists; `verify --expect sleep` | `failures: []`, 0 nodes |

On any failed gate, step 13 runs first, then step 14; the investigation happens privately.

## Why not the alternatives

| Option | Verdict |
|---|---|
| cert-manager Gateway API HTTP-01 solver | Needs cert-manager's Gateway API support enabled (an upgrade or argument change of the shared cert-manager 1.16.2) and a new ClusterIssuer, for a one-off certificate |
| certbot `--standalone`/`--webroot` on the laptop | Let's Encrypt connects to the IP the names resolve to (the GKE LB), not the laptop. The hook here writes the token into the gateway path instead |
| DNS-01 | Dynu allows no TXT records for 30 days |
| Restricted LB (source ranges) | Let's Encrypt's validators are not on any allowlist, so HTTP-01 needs the open mode at least during issuance |

## DNS, certificate lifecycle and IP

- **Ephemeral IP.** A new LB IP each public rollout. Update Dynu at step 7 and revert it at step 13. Dynu TTL is about 2 minutes (119 s observed), so changes take effect quickly. A reserved static IP is not needed for a demo window.
- **Renewal.** The certificate is valid 90 days, and the Secret and private copy survive sleep. Renewal repeats steps 3–11 in a later wake (`certbot renew` with the same hooks). Let's Encrypt limits duplicate certificates to 5 per week per name set, so use staging for every rehearsal.
- **Secrets.** The ACME account, key and certificate live in the private evidence directory (mode 700). The Secret is created from files. Nothing goes into Git.

## Public access mode

| Host | LB (open) | Envoy |
|---|---|---|
| features, registry, observe | reachable from the Internet | Basic Auth + rate limit (unchanged since G2) |
| agents | reachable at the LB | DENY unless the TCP peer is an allowed public /32-/16. Basic Auth still applies after that. If the client address were not preserved, every peer would be an internal address, which the chart refuses to allowlist, so agents stays closed (fail-closed). L5 proves which case holds |
| port 80 | reachable | redirect only; plus `/.well-known/acme-challenge/<token>` during issuance |

The open LB is accepted only with `ownership_preflight.py --allow-public-loadbalancer` and proof that the controls exist (render) and are active (live config of every gateway pod).

## Cost (list-price estimate)

About $1/h for 5 nodes, plus a few cents per hour for the forwarding rule and IP while public. Steps 1–14 take about 2 h, roughly $2–3. Nothing public is kept during sleep.
