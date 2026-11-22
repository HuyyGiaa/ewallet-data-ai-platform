# G3-B supervised closure: security gates, deadline/cancellation, ACME cleanup (offline)

Owner decision: G3-B runs supervised; unattended execution is no longer required. Offline
only: no change to GKE (0 nodes, `gke-state.txt`), Dynu, LoadBalancers or certificates. Base
5c8d2f03. The procedure is the runbook section *Supervised G3-B campaign*.

`OFFLINE_IMPLEMENTATION_READY` is not `LIVE_SECURITY_ACCEPTED`. L1–L9 still need the live run,
and L4 needs a real non-approved address.

## A. Security gates (`scripts/edge/g3b_campaign.py`)

| Gap (verified in the code) | Fix |
|---|---|
| No L0 render in the orchestrator | `L0-render` phase renders istio/gateway with the public overlays and ewallet-edge with the campaign values, then runs `ownership_preflight.py render --allow-public-loadbalancer --domain … --approved-cidrs-file …`. The same check also runs offline in `init` (`L0_RENDER_READY`). L0 live stays, before the LB |
| No private E2E before public | `private-e2e` phase: port-forward to the private gateway; `public_e2e.py --evidence-class GKE_PRIVATE --agents denied` (the port-forward peer is never approved, so 403) and `negative_tests.py --rate-limit-mode http2`. Failure means no LB |
| No L4 | `L4-denied` phase: only a real request from a non-approved address counts, either this machine on another network (checked by its public address, not recorded) or an external client's `l4-external.jsonl` with `PUBLIC_INTERNET` rows. Otherwise `L4_BLOCKED_NO_EXTERNAL_CLIENT`. A failing L4 stops the campaign |
| L5 judged without checking the client | `internet-gates` first verifies this machine is inside the approved CIDRs, else L5 FAIL |
| Grafana skipped but campaign PASS | `GRAFANA_NOT_APPROVED`, `GRAFANA_VERIFICATION_BLOCKED` (no credentials, release unreadable, `release_diff` not clean, so no upgrade), `GRAFANA_VERIFICATION_FAILED` (upgrade failed or rows 37–39 failed). PASS only when rows 37–39, including appUrl, pass |
| One PASS for everything | `verdicts()`: `G3B_DEPLOYMENT_STATUS`, `G3B_SECURITY_STATUS` (`G3B_SECURITY_COMPLETE` only with every security phase and L4 PASS), `G3B_PUBLIC_E2E_STATUS`, `G4_AUTOMATED_CHECKS_STATUS`, `CLEANUP_STATUS`, `SLEEP_STATUS`. Blocked gates show their state in `phases`, never PASS. `run` exits 0 only with security complete, cleanup complete and sleep verified |

`negative_tests.py`: the unauthenticated agents probe now accepts 401 **or 403**. 403 is the
source restriction, which runs before Basic Auth when the client is not approved, for example
through the port-forward. Other hosts stay strict 401, and anything else fails.

## B. Deadline, cancellation, locking

- **Process groups:** commands run in their own process group, with a timeout bounded by the deadline (at most 30 min each, at least 30 s); a timeout or cancellation ends the whole group. The deadline also stops new commands inside multi-command phases.
- **Signals:** SIGTERM/SIGINT ends the running command, then cleanup runs. Cleanup ignores further signals (kill -9 plus `cleanup` resumes it).
- **Cleanup budget:** cleanup is not bounded by the campaign deadline; each command keeps its own timeout.
- **Lock bug found and fixed:** `Locked.__exit__` released a lock it never acquired (`ValueError`), so a watchdog or `cleanup` would have crashed while the main process held the campaign.
- **Watchdog:** never cleans up beside a live main process. Past the deadline plus 15 min it sends SIGTERM to the main process, which then cleans up, so only one process ever changes the cluster. It cannot stop a process that is truly stuck in the kernel; the runbook gives the owner's kill -9 plus `cleanup` path.
- **`--mode supervised`:** gates `COMMON_GATES` (no watchdog, linger or laptop-risk). It still requires the manifest, PDB, KEDA, guarded sleep, client address, `SAFE_CLEANUP_READY` (DNS automation), `L0_RENDER_READY` and `OWNER_LIVE_APPROVAL`. The default stays `unattended` for compatibility.

## C. ACME responder cleanup

- **Intent states:** `ACME_STATUS` is persisted before the edge upgrade that enables the responder and before each issuance; the certbot process group is recorded.
- **First cleanup step:**
  1. let an in-flight certbot finish (at most 10 min), then end its group;
  2. upgrade ewallet-edge without `acmeHttp01`;
  3. verify no ACME object remains while the application routes and `edge-tls` are present.
- **Never:** delete `edge-tls`, roll a production certificate back, or force anything.
- **Failure:** the step is recorded (`CLEANUP_INCOMPLETE`), DNS, LB and sleep still run, and a later `cleanup` retries it.
- **Happy path:** `responder-off` verifies the same way and needs no cleanup step.

## Tests (`tests/ops/test_g3b_supervised.py`, 37)

| Group | Cases |
|---|---|
| Security | L0 render failure, private E2E failure, L0 live failure: no public LB; exact public values in L0; private E2E is GKE_PRIVATE through the port-forward with agents denied, before the LB; L4 blocked means security not complete and G4 PARTIAL; L4 from this client once outside the CIDRs; a failing external L4 file stops the campaign; L5 failure; COMPLETE only with L4 and Grafana verified; Grafana not approved, no credentials, release_diff failure (no upgrade), rows failing |
| Deadline / locking | supervised mode drops only the unattended gate and still refuses missing DNS/PDB/KEDA/live approval; L0 render gate fails offline; a timeout kills the whole process group; the deadline bounds commands; `terminate_current`; real SIGTERM and SIGINT; deadline mid-campaign starts no new step; the watchdog never cleans up beside a live main process and SIGTERMs it after the grace (and does not clean up itself) |
| ACME | staging and production failure remove the responder before DNS and LB; already disabled needs no step; cleanup failure reported, DNS/LB/sleep continue, retry completes; an in-flight certbot ended first; no delete, force or secret rewrite in cleanup; kill -9 during ACME cleaned up by the watchdog |

Simulated CLI campaigns (`simulation/`, supervised mode, synthetic inputs):

| Scenario | Commands | Result |
|---|---|---|
| happy | 48 | every gate PASS at init; deployment PASS, public E2E PASS, security INCOMPLETE (L4 blocked: no external client in a simulation), G4 PARTIAL (Grafana not approved), cleanup COMPLETE, sleep VERIFIED |
| staging fails | 43 | deployment FAIL, G4 BLOCKED, cleanup order `acme-responder-off`, `dns-offline`, `lb-release`, `record-manifest`, `sleep`, `verify-sleep`; COMPLETE |

| Regression | Result |
|---|---|
| `tests/ops` (incl. DNS/campaign 61, supervised 37, preflight, charts, Cloud Lab) | all pass (`tests/tests-ops.txt`) |
| component CI | pass |
| helm lint; G3-A1 edge render | lint OK; byte-identical |
| charts and `cloud_lab.py` | unchanged |
