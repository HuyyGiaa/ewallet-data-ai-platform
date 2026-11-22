# G3-B final cleanup safety patch (offline)

Base fc3233fe. Offline code patch plus read-only checks. There were no changes to GKE (0 nodes, 0 LoadBalancer
services), Secrets, Dynu (still Offline, `DNS_OFFLINE_OK`), certificates, Istio or Helm.
Only `scripts/edge/g3b_campaign.py`, its tests and the runbook changed.

## A. Fail-closed DNS before every LB release (P0)

Defect: cleanup started with `dns_safe = True` and ran DNS work only for `DNS_TARGET_STATE`
`online`/`unknown`. With a public LB and `untouched` (the `dns-transition` pre-check failed
before any update), the LB was released with no DNS proof. The old test accepted this.

Fix: whenever `PUBLIC_LB_CREATED` is true, cleanup runs a new read-only step
`dns-verify-before-release` before the release:

`dns_check.py --expect-offline --offline-ip OFF4 --offline-ipv6 OFF6 --released-ip <LB>`

- It runs for every recorded state (`untouched`, `unknown`, `online`, `offline`), with 3 attempts.
- If the LB address is not in the state, it is read from the Service first.
- Only `DNS_OFFLINE_OK` releases the LB. `NOT_YET` or `DNS_CHECK_FAILED` keeps it, and the verdict goes into `lb-release` ("LB KEPT: …").
- The result is `CLEANUP_INCOMPLETE` (never COMPLETE). Guarded sleep and verification still run.
- A later `cleanup` resumes and skips finished steps.
- No Dynu update is added. Signoff is still sent only for `online`/`unknown`, as before.

## B. DNS transition evidence gate (P1)

`transition_evidence()` now also requires:
- `simulated: false`: `init` and `run` mark Simulator states, and the marker is sticky;
- the `dns-transition` phase `PASS`;
- `CLEANUP_STATUS: COMPLETE`, `SLEEP_STATUS: VERIFIED`, `LAST_VERIFIED_NODE_COUNT: 0`;
- cleanup steps `dns-verify-before-release` and `lb-release` ok;

plus, as before, scope, `DNS_TRANSITION: VERIFIED`, domain, mode, OFF4 and OFF6.

## Simulation (`simulation/`, synthetic inputs, nothing executed)

| Run | Result |
|---|---|
| `dns-transition`, every `dns_check` failing | `DNS_TRANSITION_NOT_STARTED`, `DNS_TARGET_STATE: untouched`, **0 Dynu commands**, `dns-verify-before-release: DNS_OFFLINE_NOT_YET`, `lb-release: LB KEPT`, sleep + verify done, `CLEANUP_INCOMPLETE` (`precheck-fail-state.json`) |
| `cleanup` again with DNS OK | `COMPLETE` |
| G3-B `init` with a simulated VERIFIED transition as evidence | `SAFE_CLEANUP_READY: FAIL` (`['not simulated']`) |

## Tests

| Run | Result |
|---|---|
| `tests/ops/test_g3b_prelive.py` | 66 passed. New: pre-check failure (no mutation, LB kept, resume); untouched + OK releases with no Dynu call; `untouched`/`offline`/`online`/`unknown` × NOT_YET/CHECK_FAILED keep the LB (offline = drift); online: signoff, then the independent check, then release; resume; LB address lookup; simulated evidence refused; verified with incomplete cleanup (sleep failed, LB kept) refused; each evidence field required; a fully cleaned transition accepted |
| regression (G3-B, DNS, preflight, edge, charts) | 433 passed |
| full `tests/ops` | 566 passed |

## Read-only state now (`readonly-state.txt`)

GKE 0 nodes, 0 LoadBalancer services; DNS `DNS_OFFLINE_OK` (0 errors).
