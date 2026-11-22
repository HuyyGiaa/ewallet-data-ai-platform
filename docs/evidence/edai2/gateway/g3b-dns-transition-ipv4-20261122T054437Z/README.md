# G3-B DNS transition: VERIFIED (g3b-dns-transition-ipv4-20261122T054437Z)

Owner-approved supervised campaign (`--scope dns-transition`), new and not a resume. Base ef0983d3; manifest G3-A1.
Owner change before the run: Dynu *Enable IPv6 Address* OFF, *Wildcard IPv6 Alias* OFF, TTL 120.
**Verdict: G3B_DNS_TRANSITION_LIVE_VERIFIED.**

| Item | Result |
|---|---|
| preflight (`preflight.txt`) | repo clean = remote; 292 focused tests passed; 0 nodes, no LB, no forwarding rules or addresses; gateway ClusterIP; G3-A1 manifest checksums OK; Secret MATCH; DNS `DNS_OFFLINE_OK` with A = OFF4, **AAAA none** (including the wildcard); credentials 600; client inside the /32 |
| `init` (`init-output.json`) | 9/9 PASS, `EDGE_AUTH_MATCH`, no sync, no live approval |
| wake (5 nodes), baseline, temporary-certificate, edge-restricted, L0-render, L0-live | PASS (05:45:01Z) |
| edge-auth | PASS, `EDGE_AUTH_MATCH`, no mutation |
| private-e2e | PASS: `public_e2e` 8/8, `negative_tests` 35/35 |
| open-lb | PASS (L0 live with `--allow-public-loadbalancer`) |
| DNS pre-check (`dns-transition-0-precheck.jsonl`) | `DNS_OFFLINE_OK`; per row A = OFF4, AAAA none |
| DNS online (`dns-transition-1-online.jsonl`) | Dynu `good`; AAAA OK at every attempt; `DNS_ONLINE_OK` at attempt 7 (A = LB everywhere, no AAAA) |
| independent online sampling (`dns-online-observation.jsonl`) | 5 samples × 48 answers (6 names × 8 servers): **AAAA none in every answer**; A = LB in 41 answers from 05:59:32Z |
| DNS signoff (`dns-transition-2-offline.jsonl`) | `DNS_OFFLINE_OK` at attempt 5, with `--released-ip <LB>` |
| cleanup (06:02:28Z, `state-sanitized.json`) | acme-responder-off ok; dns-verify-before-release `DNS_OFFLINE_OK` (no Dynu update needed); lb-release ok; record-manifest ok; guarded sleep ok; verify-sleep nodes = 0. **COMPLETE** |
| afterwards (independent, `dns-after.jsonl`) | 0 nodes; 0 LB Services; gateway ClusterIP; 0 forwarding rules; 0 reserved addresses; `DNS_OFFLINE_OK` with `--released-ip`; ns1, 1.1.1.1 and 8.8.8.8: A = OFF4, AAAA none |
| evidence gate | `transition_evidence()` accepts this `state.json` (no problems) |

Sampling notes, kept as observed:
- The independent sampler used a guessed server list (ns1, ns3–ns7). One of them answered nothing for all 6 names (the constant 6 "none" A answers). `dns_check` uses the zone's real NS set.
- The last sample (06:00:21Z) caught 2 A answers that were neither the LB nor OFF4 while signoff was starting. No `dns_check` verdict accepted such a value, and the final state is OFF4 everywhere.

Not committed: `l0-edge.yaml` (client address), `private/`, `recovery-manifest/`, raw `state.json` (a sanitized copy is included; the raw file stays in the private campaign directory as the `--dns-transition-evidence` input).
