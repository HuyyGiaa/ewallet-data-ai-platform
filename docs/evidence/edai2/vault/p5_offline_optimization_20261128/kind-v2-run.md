# Kind V2 runs with the P5 driver (2026-11-28)

**KIND PASS on the fourth run; not GKE evidence.** Auto-unseal used kind's transit stand-in
(`SIMULATED_AUTO_UNSEAL`), and every value was synthetic.

| | |
|---|---|
| Cluster | kind `kind-llm-agent-local`. cert-manager v1.16.2 was installed for each run with `kind_cert_manager.sh` and removed afterwards |
| Software | Vault 2.0.4 (chart 0.34.1), ESO 2.14.0, `rehearsal-serving` with `localhost:5001/ewallet-inference@sha256:8a6106c3…` |
| Sequence | the §10 order without `agents` (kind-only, 40 min, unaffected by P5): `preflight vault init configure ceremony seal eso consumer feature-api switch negative rollback lifecycle retain`, `gate retain`, a simulated wake (`vault-0` and the Redis pod deleted), `after-wake`, `gate after-wake`, `cleanup`, `verify-clean`, `gate clean` |
| Final script | `scripts/vault/gke_rehearsal.sh` sha256 `755134183799d90d…` |

## Runs

| Run | ID | Result |
|---|---|---|
| 1 | `vault-rehearsal-20261128T061138Z` | stopped at `lifecycle` / `seal-down-sync`: "unexpected cause unclassified". Every earlier step passed, including the new `expect_seal` checks at init and at both restarts. Cleanup PASS |
| 2 | `vault-rehearsal-20261128T062522Z` | the same stop, reproduced. Cleanup PASS |
| 3 | `vault-rehearsal-20261128T063943Z` | the same stop, with ESO's state captured before cleanup ([`kind/run3-eso-state-at-failure.txt`](kind/run3-eso-state-at-failure.txt)). Cleanup PASS |
| 4 | `vault-rehearsal-20261128T065409Z` | **PASS**: 117 records, 0 FAIL, gates `retain`, `after-wake` and `clean` open, 8 lookups `BASELINE_OK` ([`kind/results-run4-pass.jsonl`](kind/results-run4-pass.jsonl)) |

The failure records of runs 1–3 are in [`kind/runs1-3-failure-records.jsonl`](kind/runs1-3-failure-records.jsonl).

After every run, kind was back to its 6 releases. No rehearsal namespace, cert-manager, rehearsal PV,
port-forward or private material was left.

## Root cause of the `seal-down-sync` stop

The cause was the driver's classifier, not Vault or ESO, and it predates P5.

1. In the kind-only K14 drill the transit unsealer goes offline and Vault restarts. Vault never opens
   its listener.
2. ESO then fails in one of two orders:
   - **The ExternalSecret reconciles first.** Its own log line has the network error, and `es_error`
     returns `vault-unreachable`. That happened in the 2026-11-25 run.
   - **The store reconciler marks `ClusterSecretStore ewallet-vault-rehearsal` NotReady first** ("unable
     to create client", `InvalidProviderConfig`). The ExternalSecret then logs only
     `ClusterSecretStore "ewallet-vault-rehearsal" is not ready`, and the network error is logged under
     the store's name.
3. `es_error` read only the ExternalSecret's lines, so the second order was `unclassified`. That is what
   happened in runs 1–3.

The behaviour under test was correct in all three failed runs: the ExternalSecret went `False SecretSyncedError`.

**Fix (still strict):**

- `es_error` now names that message `store-not-ready`. Specific causes such as `invalid audience` keep
  priority, so the negative tests are unchanged.
- The drill then classifies the store's own log lines.
- The step still passes only on `vault-unreachable` or `vault-sealed`.

Unit tests cover both orders and the priority. Run 4 recorded `error_class: vault-unreachable`.

**Effect on GKE:** none. The drill runs only when the context is kind (`if [ -n "$KIND" ]`); Cloud KMS is
never made unreachable.

## The P5 seal checks

| Step | `seal_type` | Checked against |
|---|---|---|
| `vault-uninitialised`, `auto-unsealed` (init) | `transit` | `seal_kind` = `transit` on kind |
| `restart-auto-unsealed`, `restart-auto-unsealed-eso-resumed` | not recorded | checked by `expect_seal` before the record |
| `rebound-initialised-auto-unsealed` (Released-PV recovery) | `transit` | the same |
| `vault-after-wake` | `transit` | `expected_seal_type: transit` |

On GKE the same checks require `gcpckms`. On kind, `gcpckms` and any other seal type are refused; the
unit tests cover the reverse.
