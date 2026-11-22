# G3-B Let's Encrypt production: bounded retry patch (offline)

Base 2dcc146c. No cloud change. The third Full G3-B run `g3b-full-20261122T083830Z` stays FAILED
(raw evidence outside Git).

## Root cause (verified read-only from that attempt's certbot log; no ACME material printed)

Requests the production attempt sent: `GET /directory`, `HEAD /acme/new-nonce`, `POST /acme/new-acct`.
Final error: `requests.exceptions.ReadTimeout` (45 s). No `new-order` request was sent (the only
"new-order" text is the URL inside the directory body), no certificate was received, and no
`config/live/edge` exists. So it was an account-registration timeout before any order.

The classifier on the real logs: production log → safe to retry; the successful staging log → no
retry (certificate received); the same log against a later attempt start → no retry (stale).

## Patch (`scripts/edge/g3b_campaign.py`)

`acme_retry_safe(log, started, live)` returns retry-safe only if all of these hold:
- `config/live/edge` does not exist;
- the log's first timestamp is not older than this attempt's start;
- the log has no "Successfully received certificate" / "Certificate is saved at";
- no request to new-order, newOrder, finalize or an order URL was sent;
- the last request sent was new-acct (or newAccount);
- the last ERROR line is `requests` ReadTimeout/ConnectTimeout (or the urllib3 equivalent).

The production phase also requires no signal (`stop_requested`), at least 20 min before the
deadline, and that the first certbot process group is no longer alive.

If all hold, it waits 60 s (interruptible, checked every second) and makes exactly one more attempt
with the same certbot configuration and directories; at most 2 attempts, sequential. Staging and
every other failure are unchanged and fail closed.

State fields: `ACME_PRODUCTION_ATTEMPTS`, `ACME_PRODUCTION_RETRY_REASON` (or `no retry: <reason>`),
`ACME_PRODUCTION_FINAL_STATUS`. No ACME material is recorded.

## Tests

| Run | Result |
|---|---|
| `tests/ops/test_g3b_acme_retry.py` (new) | 19 passed: retry then success; retry then failure (2 attempts, cleanup); unauthorized, HTTP-01 failed, rate-limited, timeout after order, timeout with no request, timeout at nonce, received but import failed, stale log, no timestamp → no retry; certificate on disk; missing log; deadline close; signal; staging never retried; first process group alive; sequential with one 60 s delay; third-run log shape → safe |
| G3-B regression | 456 passed |
| full `tests/ops` | 651 passed |

## Read-only state

| Check | Result |
|---|---|
| third campaign | CLEANUP COMPLETE, SLEEP VERIFIED, 0 nodes |
| GKE | 0 nodes; no LB Service; 0 forwarding rules |
| DNS | DNS_OFFLINE_OK (one transient DNS_CHECK_FAILED: ns4.dynu.com unanswered over this laptop's IPv6, then two clean OK runs) |
| edge Secret | new `edge-cred` MATCH; retired MISMATCH |

**Observation (not fixed here; owner decision):** this laptop has global IPv6 addresses, but IPv6
to Let's Encrypt (production and staging) fails, while IPv4 answers in under 1 s. certbot's Python
HTTP stack generally tries addresses in resolver order, so a broken IPv6 path is a likely cause of
the production timeout. The bounded retry does not fix a persistently broken IPv6 path.
