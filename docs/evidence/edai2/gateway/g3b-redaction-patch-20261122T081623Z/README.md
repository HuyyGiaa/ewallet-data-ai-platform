# G3-B credential-leak remediation and Loki selector fix (offline)

Base 4f82d785. No cloud, Secret, Helm, DNS or certificate change. The second Full G3-B run
`g3b-full-20261122T074035Z` stays FAILED (its raw evidence is outside Git).

## Root causes

1. **Credential leak:** `negative_tests.Edge.request()` and `Edge.burst()` stored curl's `%{redirect_url}` verbatim. curl builds that absolute URL from the request URL, which carries the `-K` user credentials. `public_e2e.grafana_checks` recorded `reply.location` in row 37, and nothing scrubbed results before stdout or JSONL.
2. **Loki row 38:** the default selector `{namespace="ewallet-agents"}` uses a label Loki does not have. Alloy/OpenTelemetry label names are `k8s_namespace_name` (verified read-only on GKE: 20 lines).

## Fix

| Layer | Change |
|---|---|
| shared client (`negative_tests.py`) | `redact_url()` drops URL userinfo and keeps scheme, host, port, path, query and fragment; relative URLs are unchanged. Applied to `Reply.location` in `request()` and to every transfer in `burst()`. Redirect checks keep working (host comparison unchanged) |
| serialization (both scripts) | `sanitize_text()` removes any URL userinfo and replaces every form of the edge and (public_e2e) Grafana credentials (raw, URL-encoded, Basic base64; `secret_forms()`) with `<redacted>`, before JSONL and before stdout / FAIL lines |
| Loki | `public_e2e.py --loki-query` default `{k8s_namespace_name="ewallet-agents"}`; override kept; runbook examples updated |
| runbook | edge-credential rotation procedure (owner, local, never displayed) and the redaction note |

## Tests

| Run | Result |
|---|---|
| `tests/ops/test_edge_credential_redaction.py` (new, synthetic credentials) | 20 passed: absolute, encoded, encoded with port and fragment, none, other host, relative, port, IPv6, "@" in query, empty; request and HTTP/2 burst redaction; Grafana redirect row keeps its verdict (same host PASS, other host FAIL, relative PASS) without the credential; `sanitize_text` forms; public_e2e stdout + JSONL; negative_tests FAIL lines + JSONL; Loki default and override |
| G3-B regression (10 files) | 437 passed |
| full `tests/ops` | 632 passed |
| credential forms in test outputs (real or synthetic) | none |

## Evidence leak scan (counts only, no values printed)

1200 files inspected (gateway campaign evidence outside `private/`, committed gateway evidence,
scripts, tests, runbooks), covering the edge and Grafana credentials (raw, URL-encoded, Basic
base64) and any URL with userinfo: **0 findings**. Git-tracked files with a real credential form: 0.
The earlier redaction of `g3b-full-20261122T074035Z/07-private-e2e.jsonl` is complete
(`REDACTION.txt`).
The edge password did appear once in a session tool output, so rotation is recommended (runbook procedure; Secret sync needs owner approval in the next campaign).
