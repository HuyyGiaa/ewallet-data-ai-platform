# G3-B final pre-live closure: edge Basic Auth sync and the first DNS transition (offline)

Base 2af465b4. Offline code patch plus read-only checks. There were no changes to GKE (0 nodes, no LoadBalancer), the
`edge-basic-auth` Secret, Dynu (still Offline, `DNS_OFFLINE_OK`), certificates or Grafana. The procedure
is in the runbook: *Stages and their approvals*, *First DNS transition*, and procedure step 6a.

## A. Edge Basic Auth Secret synchronisation (P0)

Problem: `private-e2e` uses the owner's new `edge-cred`, but the campaign never touched the
Secret. The owner measured `EDGE_AUTH_MISMATCH`, and this patch's read-only check returns the
same (`readonly-state.txt`). The live run would have failed private authentication.

| Piece | Change |
|---|---|
| `create_basic_auth.sh` | new `--credentials-file FILE --check \| --apply` mode (the stdin mode is unchanged for the local validators). The file must be mode 600 with exactly one `user:password` line and a password of at least 24 characters. The same `{SHA}` hashing produces the line. `--check` is read-only and prints only `EDGE_AUTH_MATCH` / `MISMATCH` / `MISSING` / `CHECK_FAILED` (exit 0/1/1/2). An extra user also counts as a mismatch. The password never appears in arguments, environment or output; the hash is only compared in memory or piped to `kubectl apply` |
| `g3b_campaign.py init` | `EDGE_AUTH_READY` gate: PASS on a match, or on a mismatch with `--approve-edge-secret-sync`. `edge_auth_at_init` records the verdict word |
| `g3b_campaign.py` phase `edge-auth` (after L0-live, before private-e2e) | match: no mutation. Mismatch without approval: FAIL, no LB. Mismatch with approval: `--apply`, `rollout restart` + `rollout status` of `ewallet-edge-gateway` (Envoy reads the file at start-up), `platform_checks gateway`, `--check` again |
| `private-e2e` (unchanged; now documented as the proof) | real requests: valid credentials accepted on the allowed routes; missing, wrong, unknown-user, bearer and forged-header requests 401 (agents 401/403); agents 403 with valid credentials (source restriction); HTTP/2 burst 429. A gateway still holding the old users file fails here, so no LB |

## B. Safe DNS Online/Offline transition (P0)

Facts:
- The owner's API `offline=yes` was sent while the hostname was already Offline (`good`, `DNS_OFFLINE_OK`), so it proves nothing about Online → Offline.
- Dynu's protocol page documents `offline=yes` only. It documents no `offline=no`, and says nothing on whether `myip=` brings a hostname back online.
- The earlier runbook plan (panel *Go Online* while GKE sleeps) could publish an address the owner does not control. It is withdrawn.

Design (the smallest safe option): a separate supervised **`--scope dns-transition`** in the
same orchestrator, using the campaign's own GKE LB address:
- It runs the phases up to `open-lb` unchanged (wake, baseline, temporary certificate, edge, L0 render/live, edge-auth, private-e2e). The LB opens only on a checked edge.
- `dns-transition`:
  1. pre-check DNS offline (OFF4/OFF6), else no update is sent;
  2. `dynu_dns online --ip <LB>`, then `DNS_ONLINE_OK` (A = LB, no AAAA);
  3. `dynu_dns offline --mode signoff --offline-ip --offline-ipv6 --released-ip <LB>`, then `DNS_OFFLINE_OK`;
  4. `DNS_TRANSITION=VERIFIED`.
  Every step is decided by `dns_check`, never by Dynu's answer word. Outputs are written to `dns-transition-*.jsonl`.
- Then normal cleanup. A failure leaves DNS online/unknown, so cleanup sends signoff and releases the LB only when offline is proven; otherwise the LB is **kept**, the nodes sleep, and the result is `CLEANUP_INCOMPLETE`. Owner fallback: panel *Go Offline Now*, then `cleanup`.
- Its own gates: `TRANSITION_GATES`, with `DNS_TRANSITION_TEST_READY` (supervised, signoff, both offline addresses, DNS credentials, hostname proven offline now) and `OWNER_DNS_TRANSITION_APPROVAL` (`--approve-dns-transition-test`). There is no `SAFE_CLEANUP_READY` and no `OWNER_LIVE_APPROVAL`; the G3-B live approval does not unlock it. G3-B/G4 verdicts are `NOT_RUN (dns-transition scope)`.
- In the full G3-B scope, `--dns-offline-verified` alone no longer passes `SAFE_CLEANUP_READY`. It also needs `--dns-transition-evidence <state.json>` with `scope: dns-transition`, `DNS_TRANSITION: VERIFIED` and the same domain, mode, OFF4 and OFF6. Signoff now requires both offline addresses.

## Read-only state now

| Check | Result |
|---|---|
| private files | 4 files, mode 600 (metadata only) |
| Secret vs `edge-cred` | `EDGE_AUTH_MISMATCH` (sync needs owner approval) |
| GKE | 0 nodes, 0 LoadBalancer services in `ewallet-ingress` |
| DNS (`dns-offline-now.jsonl`) | `DNS_OFFLINE_OK` with 162.216.242.207 / 2602:ff23:0:8888::207, 0 errors |

## Simulation (`simulation/`, synthetic inputs, Simulator: nothing executed)

| Run | Result |
|---|---|
| `dns-transition`, Secret mismatch + approved sync | init all PASS; `EDGE_AUTH_SYNCED`, private-e2e PASS, `DNS_TRANSITION: VERIFIED`, cleanup COMPLETE, sleep VERIFIED; order in `transition-command-order.txt` |
| G3-B init, `--dns-offline-verified` without evidence | `SAFE_CLEANUP_READY: FAIL` ("needs --dns-transition-evidence") |
| G3-B init with the simulated transition `state.json` | `SAFE_CLEANUP_READY: PASS` |
| mismatch without `--approve-edge-secret-sync` | `EDGE_AUTH_READY: FAIL` |

## Tests

| Run | Result |
|---|---|
| `tests/ops/test_g3b_prelive.py` (new) | 46 passed: script against a fake kubectl (match, mismatch, missing, extra user, unreadable Secret, unsafe or malformed files refused before kubectl, apply round trip, stdin mode, no password or hash in arguments or output); campaign edge-auth (init gate, no mutation on match, unapproved mismatch, approved order, sync not taking, restart or behaviour failure means no LB, password absent from commands, state and files); dns-transition (gates, approval separation, offline pre-check, happy cycle and order, no update when not offline, online not proven, signoff failure keeps the LB and resumes, propagation never completing); evidence-backed `--dns-offline-verified` |
| regression (G3-B, DNS, preflight, edge, charts) | 413 passed |
| full `tests/ops` | 546 passed |

`tests/ops/test_edge_negative_checks.py`: a pre-existing flaky assertion was fixed. The synthetic password `pw` could appear inside a random temporary directory name; it is now a distinctive string.
