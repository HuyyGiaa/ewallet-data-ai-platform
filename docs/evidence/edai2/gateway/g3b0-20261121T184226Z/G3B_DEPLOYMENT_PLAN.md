# G3-B public deployment plan (not executed; owner approval required)

Builds on the retained private edge from G3-A1. No new gateway, controller or LB type. Every
step is a GKE or DNS mutation that needs the owner's explicit approval.

## Inputs the owner must supply

| Input | Status | Why |
|---|---|---|
| Domain `<D>` the owner controls | `PENDING_OWNER_DOMAIN` | Hostnames `agents.`, `registry.`, `observe.`, `features.<D>`; certificate SANs; Grafana root URL |
| Client CIDRs (owner and graders, public IPv4) | pending | `loadBalancerSourceRanges` and `sourceRestriction.allowedCidrs` |
| Certificate for `*.<D>` (or the four names) | pending | `edge-tls` Secret |
| Approval of the Grafana diff (`grafana/grafana-render.diff`) | pending | frozen chart |

Read-only facts (2026-11-21, `tls-dns-readonly.txt`):
- no ClusterIssuer; cert-manager 1.16.2 runs without Gateway API support;
- no Cloud DNS zone and no reserved static IP in the project.

## TLS: recommended path

| Option | Fit |
|---|---|
| **Owner-supplied wildcard via ACME DNS-01, issued off-cluster** (for example `certbot certonly --manual --preferred-challenges dns -d '*.<D>'`, TXT record at the owner's registrar) | **Recommended.** No cluster change: the chart already takes an owner-supplied Secret (`tls.certificate.enabled: false`). Works with the source-range-restricted LB. A 90-day certificate covers the demo |
| cert-manager HTTP-01 through the Gateway | Not workable as is. The ACME validators come from addresses the source ranges block. It would also need cert-manager's Gateway API support (a cert-manager upgrade) and a ClusterIssuer. The challenge route itself would work (same namespace, an exact path beats the redirect route, and port 80 has no Basic Auth), but opening port 80 to the world defeats Option A |
| cert-manager DNS-01 | Needs a DNS provider API credential in the cluster (no Cloud DNS zone exists). More moving parts than a one-off manual issuance |

The private key stays outside the repository and goes into the Secret through `kubectl create secret tls --dry-run=client -o yaml | kubectl apply`.

## DNS

Four A records (or one wildcard `*.<D>`) pointing to the LB address, with a low TTL (300 s)
during the demo. Two choices:
- **Ephemeral IP:** free while unused, but the address changes on every public rollout, so the records are updated each time.
- **Reserved regional static IP:** stable records; set `service.loadBalancerIP`. The address is billed while reserved and unused (list price, to be checked).

Remove or repoint the records when the LB is removed, so no record points to a recycled address.

## Rollout order (fail-closed; no public moment without active filters)

1. `wake-full --manifest ~/ewallet-evidence/gateway/g3a1-20261121T175830Z/11-sleep/recovery-manifest --nodes 5`, then the G3-A1 health checks.
2. Private re-check over port-forward: Envoy chain `local_ratelimit → basic_auth → router` on 443, auth 401/200, kagent-ui Ready.
3. Replace `edge-tls` with the real certificate. Over port-forward, verify **without** `--cacert` (system trust) for all four names.
4. `helm upgrade ewallet-edge -f g3a-private-values.yaml --set domain=<D> --set sourceRestriction.enabled=true --set-json 'sourceRestriction.allowedCidrs=[...]'`. Still ClusterIP. Over port-forward, agents must now return 403: the peer is the port-forward, not an allowed address (live KA-01).
5. If approved: `helm upgrade llm-agent … --set grafanaExternal.rootUrl=https://observe.<D>/` (one env line; only Grafana restarts).
6. Verify the active config before exposure: config dump (filters, buckets, no retry), `edge-source-restriction` present, ownership preflight live OK.
7. `ownership_preflight.py render --allow-loadbalancer ewallet-ingress/ewallet-edge-gateway` on the public render, then `helm upgrade ewallet-edge-gateway -f values-gateway.yaml -f values-gateway-public.yaml --set-json 'service.loadBalancerSourceRanges=[...]'`. The same pod keeps its active filters; only the Service changes.
8. Wait for EXTERNAL-IP, then create the DNS records.
9. Live gates (below), then the G4 evidence.
10. End of campaign: `helm upgrade ewallet-edge-gateway -f values-gateway.yaml` (back to ClusterIP; GKE removes the LB). Verify no LoadBalancer. Remove the DNS records. Record a fresh manifest, `plan-sleep`, guarded sleep, `verify --expect sleep`.

## Live acceptance gates (G3-B)

| ID | Gate | Pass condition |
|---|---|---|
| L1 | Certificate | valid chain with system trust, SAN matches every hostname, TLS 1.1 refused |
| L2 | Auth | 401 with challenge; valid credentials → application; forged headers 401 |
| L3 | Rate limit | `negative_tests.py --rate-limit-mode http2` against the public address: verdict PASS |
| L4 | LB firewall | from a non-allowed network (for example Cloud Shell or a phone hotspot): connection times out on 80/443 |
| L5 | Source IP preserved | with only the laptop's address in `allowedCidrs`, agents is served to the laptop (a SNAT'd peer would be denied). Optional: temporarily add a second network to the LB ranges only, and that network must get 403 on agents and 401/200 elsewhere |
| L6 | Internal services | the 11 internal hostnames and undeclared paths are not routed (harness) |
| L7 | Ownership | one gateway, the one approved LB, no other ingress |
| L8 | Regression | the G3-A1 regression checks (kagent, registry, Jenkins→registry, Grafana datasources) |
| L9 | Grafana | `appUrl` = `https://observe.<D>/`, login and redirects on the public host |

Any failure: step 10 rollback first (ClusterIP), then investigate privately.

## Rollback

| Change | Undo |
|---|---|
| Public LB | `helm upgrade ewallet-edge-gateway -f values-gateway.yaml` (ClusterIP); GKE deletes the forwarding rule and firewall rule |
| Source restriction | upgrade `ewallet-edge` without `sourceRestriction` (policy removed) |
| Grafana | upgrade `llm-agent` with `grafanaExternal.rootUrl=""` |
| Certificate | restore the self-signed `edge-tls` |

CRDs, the private edge and backends stay.

## Cost (estimate, list prices)

- Awake: about $1/h for 5 nodes (G3-A1 measured $0.43 for 26 min).
- LB forwarding rule and public IP: a few cents per hour while public.
- Keeping the LB during sleep would keep billing the forwarding rule, which is why step 10 returns the Service to ClusterIP.
