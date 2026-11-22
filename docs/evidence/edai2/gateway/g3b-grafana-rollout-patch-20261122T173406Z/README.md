# G3-B Grafana rollout readiness patch (offline)

Base 1d12b715. No cloud change. The sixth Full G3-B run `g3b-full-20261122T164612Z` stays FAILED
(raw evidence outside Git).

## Root cause

18/19 phases passed (production certificate issued and trusted, root URL applied, `llm-agent` revision 61).
`public-e2e` got HTTP 503 for rows 37-39. The `grafana` phase's successful `helm upgrade` changed
Grafana's environment and replaced its pod; `public-e2e` started before the new pod was Ready.
About 55 s later Grafana was Ready 1/1 with the public root URL (read-only check during that run).

## Patch (`scripts/edge/g3b_campaign.py`, `grafana` phase only)

After a successful upgrade, read-only:
1. `kubectl -n <namespace.name from the release values, default ewallet-agents> rollout status deployment/grafana --timeout=<budget>s`;
2. `.status.readyReplicas` >= 1.

The budget is `min(300 s, time left before the deadline)`. No time left, or a signal, means
FAIL with no wait. `ROOT_URL_APPLIED` is recorded only after the check passes. Not Ready means the
phase FAILS, `public-e2e` does not run, and cleanup and sleep run. A failed upgrade is unchanged
(no wait, `upgrade exit N`). The two `release_diff` checks and the upgrade command are unchanged.
Deployment name `grafana` and namespace come from `infra/helm/llm-agent-platform/templates/observability-stack.yaml`
(`namespace.name`; the live release uses `ewallet-agents`).

## Tests

| Run | Result |
|---|---|
| `tests/ops/test_g3b_grafana_rollout.py` (new) | 12 passed: normal rollout ordered upgrade → rollout → ready check → public-e2e (Grafana PASS); delayed rollout waited; rollout timeout, complete but 0 ready, missing ready field, unreadable → FAIL, no public-e2e, cleanup COMPLETE; failed upgrade → no wait, old status; release_diff and upgrade unchanged, Grafana commands read-only; budget capped by the deadline; no wait after the deadline or a signal; a real SIGTERM during the wait → stopped, no public-e2e, cleanup COMPLETE; namespace from the release values |
| G3-B regression (12 files) | 468 passed |
| full `tests/ops` | 663 passed |

## Certificate reuse assessment (read-only; workflow not changed)

- The `temporary-certificate` phase runs `issue_certificate.sh --environment self-signed --import` on every campaign. That **replaces `ewallet-ingress/edge-tls`** (today the Let's Encrypt production certificate from run 6, valid until 2027-02-20) with a 1-day self-signed certificate, until the production phase imports a new one.
- certbot uses `--config-dir <this campaign's private dir>/tls/config`, which is fresh per campaign. So each campaign registers a new ACME account and **orders a new production certificate**; it never reuses or renews run 6's lineage. That lineage is kept in run 6's private directory (`config/live/edge`).
- Rate limits: Let's Encrypt allows 5 duplicate certificates (same 4 names) per 7 days. Run 6 used one. Each further full campaign this week uses another, so about 4 more within the week before issuance would be refused. There are also 5 failed validations per hour per account and hostname, and 10 new accounts per IP per 3 h.
- Preservation: if a campaign fails before the production phase, `edge-tls` is left with the self-signed certificate (the gateway is ClusterIP, so no public impact). The run-6 production certificate still exists in its private directory, but is not re-imported automatically.
