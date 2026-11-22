# G3-B final two fixes (offline)

Focused patch after the supervised-closure audit. Base fe67d2bb. No change to GKE (0 nodes),
Dynu, LoadBalancers, certificates, Helm charts, Cloud Lab, `public_e2e.py` or
`ownership_preflight.py`. Files changed: `scripts/edge/g3b_campaign.py`,
`tests/ops/test_g3b_supervised.py` and the runbook's L4/cleanup text.

## Fix A: L4 accepts only a verified external source (Option A)

- **Problem.** `l4-external.jsonl` from a second machine could mark L4 PASS on `row 40`, `passed` and the evidence class alone. A stale or fabricated file would have passed.
- **Change.** The file is no longer read at all. L4 passes only when all of these hold:
  1. this laptop's public address is outside the approved CIDRs (checked client-side, not recorded);
  2. the previous result is deleted;
  3. a real request with valid credentials goes to the public LB (`public_e2e.py --agents denied`);
  4. the agents host answers 403 in the result this request wrote, labelled PUBLIC_INTERNET;
  5. the address is still outside afterwards.

  Otherwise the result is `L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE`, never PASS. A 200 for agents from outside is `FAIL`, which gives security FAIL and stops the campaign. The address check only decides whether the test may run; the acceptance is the gateway's 403.
- **Tests:**
  - approved client stays BLOCKED (with or without a stale passing result);
  - an external file, passing or failing, is ignored (BLOCKED);
  - a stale file is removed before the request, and an empty result is BLOCKED;
  - flipping back to the approved network during the request gives BLOCKED;
  - the wrong evidence class gives BLOCKED;
  - a non-approved client with 403 gives PASS;
  - agents reachable from outside gives FAIL and security FAIL.

## Fix B: ACME cleanup keeps every approved route and edge-tls

- **Problem.** `acme_objects_gone()` required only `feature-api`, `agent-registry` and `https-redirect`, and found ACME objects by the substring "acme".
- **Change.**
  - Expected routes come from the approved campaign values (`g3a-private-values.yaml` plus the campaign's final values): every enabled route plus `https-redirect`, which here means all four plus redirect. An owner-disabled route is not required.
  - The four temporary objects are checked by exact identity: `httproute.gateway.networking.k8s.io/acme-http01`, `deployment.apps/edge-acme-responder`, `service/edge-acme-responder`, `configmap/edge-acme-responder`.
  - `edge-tls` must read back exactly as `secret/edge-tls`.
  - Nothing is deleted or forced. A failure makes cleanup `CLEANUP_INCOMPLETE`, and a replay completes it.
- **Tests:**
  - all routes present: PASS;
  - kagent-ui, grafana, agent-registry, feature-api or https-redirect missing: FAIL;
  - any one of the four ACME objects left: FAIL;
  - edge-tls missing: FAIL;
  - a disabled route in the approved values is not required;
  - an unrelated name containing "acme" is not mistaken for the responder;
  - a failed check gives INCOMPLETE, and the replay gives COMPLETE.

## Results

| Run | Result |
|---|---|
| focused (L4 and ACME tests) | 27 passed |
| `test_g3b_supervised.py` | 57 passed |
| required regression (supervised, dns_campaign, public_preflight, edge_scripts, negative_checks, session_a_charts) | 327 passed |
| full `tests/ops` | 460 passed |

`OFFLINE_IMPLEMENTATION_READY`, not `LIVE_SECURITY_ACCEPTED`.
