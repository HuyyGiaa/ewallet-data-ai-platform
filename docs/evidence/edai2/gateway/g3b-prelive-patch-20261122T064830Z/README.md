# G3-B minimal pre-live patch (offline; source, tests, runbook)

Base 6dbc0aed. No GKE, DNS, LB, certificate, Secret or Grafana change. Simulation is not GKE
runtime evidence. Full G3-B has not run.

| Change | Where | Behaviour |
|---|---|---|
| Owner evidence window | `g3b_campaign.py` phase `evidence-window`, `--evidence-window-minutes` (default 0, max 60), `continue` command, `status` fields | Opt-in for `--scope g3b --mode supervised` only (otherwise an init problem). It is the last phase, so a failed public-e2e never reaches it. It holds the edge and runs no command. It closes on `continue` (accepted only while open; exclusive-create marker file, never a state.json write; a marker left from before is removed when the window opens), on expiry, on the deadline minus a 30-min cleanup reserve (skipped if already inside it), or on SIGTERM/SIGINT; then cleanup runs. Recorded as its observed state, never PASS; verdicts unchanged |
| Return to the approved network | phase `approved-network` after `L4-denied` | Up to 15 min (30 s polls) for a verified client address inside the approved CIDRs. Outside or unverifiable means FAIL and cleanup. L4 itself is unchanged |
| IPv4 client lookup | `MYIP_LOOKUP` = `dig -4 +short myip.opendns.com @resolver1.opendns.com` (both lookups) | The gateway sees IPv4; the address is never stored |
| Certbot preflight | init gate `CERTBOT_READY` (g3b scope) | `uvx --from certbot==5.10.0 certbot --version` must answer `certbot 5.10.0`; `issue_certificate.sh` unchanged |
| Runbook | `EXTERNAL_GATEWAY_RUNBOOK.md` | PATH and environment preflight, full `init` command, status watch, hotspot and return steps, evidence window and `continue`, screenshot table for rows 37–43, deadline budget, Grafana root URL persistence, incomplete-cleanup recovery, automated PASS vs screenshot EVIDENCE_COMPLETE |

## Tests

| Run | Result |
|---|---|
| `tests/ops/test_g3b_evidence_window.py` (new) | 28 passed (`tests/focused-verbose.txt`) |
| G3-B regression (evidence window, supervised, prelive, dns_campaign, dns_dualstack, edge_negative_checks) | 320 passed |
| full `tests/ops` | 601 passed |

Two existing tests were updated for the new phase: the complete-security test now returns the
client to the approved address after L4, and the phase-order test lists `approved-network`.

## Simulation (`simulation/`, synthetic inputs, Simulator, real 1-minute wait)

| Step | Result |
|---|---|
| init with `--evidence-window-minutes 1` | all gates PASS, `CERTBOT_READY: PASS` (Simulator answer) |
| `continue` before the run | `CONTINUE_REJECTED` |
| status during the window | `EVIDENCE_WINDOW: open`, `EVIDENCE_WINDOW_REMAINING_MIN: 1.0` |
| `continue` twice | `CONTINUE_ACCEPTED`, then `CONTINUE_ALREADY_REQUESTED` |
| result | `evidence-window: closed: owner continue`; cleanup COMPLETE; sleep VERIFIED |
| `continue` after | `CONTINUE_REJECTED` |

Limits: the Simulator writes no Grafana or L4 result rows, so GRAFANA and L4 are not PASS in the
simulation. Those are covered by unit tests and must come from the live run.
