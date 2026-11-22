# G3-B DNS transition, supervised live retry (g3b-dns-transition-retry-20261122T050543Z)

New owner-approved campaign (not a resume). Base 5377cb8a; manifest G3-A1.
**Verdict: G3B_DNS_TRANSITION_PARTIAL**: private security PASS; Offline → Online only partly proven (A yes, AAAA no); Online → Offline proven; cleanup COMPLETE; 0 nodes.

## Pre-wake (read-only)

| Check | Result |
|---|---|
| repo | 5377cb8a = remote, clean; no campaign or port-forward running; focused tests 174 passed |
| manifests (`manifest-comparison.txt`) | no material drift: same 83 workloads and replicas, identical buckets; G3-A1 used |
| cluster | 0 nodes; gateway ClusterIP; no LB, no forwarding rules; edge rev 3; KEDA paused with the Cloud Lab marker (wake-full unpauses any marked object); `plan-wake` 5 nodes, about $0.83/h |
| Secret | `EDGE_AUTH_MATCH`, so no `--approve-edge-secret-sync` |
| DNS | `DNS_OFFLINE_OK`; authoritative A = OFF4, AAAA = OFF6; PTR `offline.dynu.com` |
| credentials / client | 4 files mode 600; client inside the /32 |
| `init` (`init-output.json`) | 9/9 gates PASS |

## Live run (05:06:06Z → cleanup 05:24:37Z)

| Phase | Result |
|---|---|
| wake (5 nodes), baseline, temporary-certificate, edge-restricted, L0-render, L0-live | PASS |
| edge-auth | PASS, `EDGE_AUTH_MATCH`, no mutation |
| private-e2e | **PASS**: `public_e2e` 8/8 (TLS verify + 4 SANs, TLS 1.1 refused, Feature API 422, agents 403, registry); `negative_tests` **35/35** (401 for missing/wrong/unknown/bearer/forged; valid 422; 11 unserved hosts + `unknown/`: anonymous 401, authenticated 404 with certificate verification; undeclared paths 404; HTTP/2 burst with 429s, recovery) |
| open-lb | PASS (one LB on `ewallet-edge-gateway`, L0 live with `--allow-public-loadbalancer`) |
| dns-transition | pre-check `DNS_OFFLINE_OK`; online: Dynu `good`; A = LB on every server from attempt 6 (about 2.5 min); **AAAA present for all 10 attempts**, so `DNS_TRANSITION_ONLINE_NOT_PROVEN` (`dns-transition-*.jsonl`) |
| AAAA value (`dns-online-state-observation.txt`) | ns1 while Online: A = LB, **AAAA = OFF6** (Dynu's offline IPv6, not a home address) |

## Cleanup (fail-closed, `state-sanitized.json`)

| Step | Result |
|---|---|
| acme-responder-off | ok |
| dns-offline (signoff from the real Online state) | ok: `DNS_OFFLINE_OK verified` (with `--released-ip <LB>`) |
| dns-verify-before-release (fresh check) | ok: `DNS_OFFLINE_OK` |
| lb-release | ok: ClusterIP, no LoadBalancer |
| record-manifest, guarded sleep, verify-sleep | ok, nodes = 0 |
| afterwards (independent) | 0 nodes; gateway ClusterIP; 0 LB Services, 0 forwarding rules, 0 reserved addresses; `dns-after.jsonl` `DNS_OFFLINE_OK` with `--released-ip` |

## What this proves

- Dynu Offline → Online via the API: **A yes** (`myip=<LB>` takes the hostname out of Offline for IPv4); **AAAA no** (`myipv6=no` leaves Dynu's offline IPv6 in place).
- Dynu Online → Offline via API signoff: **yes**, from a real Online state, proven by `dns_check` at every Dynu NS and public resolver. This is the property cleanup depends on.
- The online policy (no AAAA) was not met, so `DNS_TRANSITION` is not VERIFIED and this state.json is not evidence for `--dns-offline-verified`.

## Owner decision needed (no code change made)

How should AAAA look when the hostname is online?
1. Clear or disable the hostname's IPv6 in the Dynu panel, then retry.
2. Accept AAAA = OFF6 as the documented online state. That needs a dns_check online-policy change and owner approval. IPv6-preferring clients would see Dynu's offline page.

Omitting `myipv6` is not acceptable: Dynu could publish the updater's own (home) IPv6.

Not committed: `l0-edge.yaml` (client address), `private/`, `recovery-manifest/`, raw `state.json`.
