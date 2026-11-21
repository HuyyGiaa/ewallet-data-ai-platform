# G2 test hardening (after the G2.2/G2.3 audit)

An audit of commit `9b47cd95` found checks that could report a pass on a wrong answer. This
campaign fixes the tests only: the chart, the Gateway architecture and the frozen Agent
Platform are unchanged (`git diff 9b47cd95 -- infra/helm/ewallet-edge` is empty). It ran on
kind (`kind-llm-agent-local`, Istio 1.30.7 ambient, Gateway API v1.7.0 Standard CRDs) with
the synthetic echo stub. GKE stayed asleep and was not contacted. The earlier evidence
directories are untouched.

## What was wrong and what changed

| # | Check | Before (false positive) | After |
|---|---|---|---|
| 1 | Limit is per route | `status != 429`: HTTP 0, 401 or 500 passed | the other route must reach its backend (stub 200 echo; real Feature API 422 with correlation id) |
| 2 | Recovery after the burst | `status != 429` | the registry must answer through the edge again (`registry_served`) |
| 3 | Unauthenticated burst | only "no 200": 0, 403 or 500 passed | every reply is 401 or 429 (the limiter runs before Basic Auth) |
| 3b | Authenticated burst | `codes ⊆ {200, 429}`: a run where every reply was 429 passed | at least one served reply, at least one 429, nothing else |
| 4a | Broken users file, running proxy | anonymous `!= 200` | anonymous exactly 401, the real password still reaches the stub, basic_auth still active on 443, "Listener rejected" in the proxy log |
| 4b | Broken users file, restarted proxy | anonymous `!= 200` | exactly one Ready pod (the old one), the new pod NotReady with a rejected listener and no `0.0.0.0_443`, via the Service anonymous 401 and valid credentials served |
| 5 | Credentials in `curl -K -` | `user = "<raw>"`: a `"` or `\` changed or broke the password | one escaping function (`curl_user_config`) used by the Python checks and the shell script; a line break is refused |
| 6 | Undeclared hosts (found in review) | anonymous `0/401/404`, authenticated `0/404`: a dead listener passed | anonymous exactly 401 (404 before the filters exist), authenticated exactly 404 |
| 7 | HTTP redirect (found in review) | 301 only | also: the target is HTTPS on the same host |
| 8 | Intruder probes (found in review) | any exception was `BLOCKED`, including a DNS failure wrapped in `URLError` | only a timeout, reset or refused connection counts |
| 9 | Registry success (requested) | stub 200 only | stub: 200 with the echo body. Real UI (G3): 200, or a 301/302/303/307/308 to HTTPS on `registry.<domain>`. A redirect to another host, a downgrade to HTTP, 304 and any other status fail |

The shell run now uses a password containing a double quote, a backslash, an apostrophe and
spaces, so every client (Python checks, shell probes, htpasswd creation) is tested with it.

`ownership_preflight.py` stays read-only. It never deleted anything, but an existing
owner-approved Gateway could not be approved. `--allow-gateway NS/NAME` now does that,
covering both the Gateway and the workload Istio provisioned for it, the same way
`--allow-loadbalancer` already worked. A finding now prints "Nothing was changed. Review
each finding".

## Results (final scripts)

| Run | Result |
|---|---|
| Focused unit tests (`unit/focused-tests.txt`): HTTP 0/401/429/500, redirects, curl round trip through a real local server | 50/50 |
| The same tests against the old assertions (`unit/old-assertions-mutation.txt`) | 16 fail, as intended: each one is a false positive the old code would have passed |
| Component CI (`unit/component-ci.txt`): pytest incl. chart, preflight and focused tests; unittest | 128/128, 63/63 |
| `foundation/`: status, HTTP | 14/14, 32/32 |
| `full/status.jsonl`: API status, ownership, Envoy config before/after restart, TLS, network, fail-closed | 26/26 |
| `full/results.jsonl` / `full/results-after-restart.jsonl` | 48/48, 48/48 |
| `static/static-checks.txt`: helm lint, guards, render kinds, preflight render | all as expected |

Measured on the final full run: authenticated burst 56 served / 64 × 429 (after restart
56 / 64), unauthenticated burst 401 = 3, 429 = 27 (after restart 4 / 26), other = none. Probes:
control `REACHED`; other namespace and unlabelled gateway-namespace pod `BLOCKED
TimeoutError`; label-spoofed pod `BLOCKED ConnectionResetError` (ztunnel, AuthorizationPolicy).
Fail-closed: running proxy anonymous 401, valid credentials served, basic_auth active, 2
rejected-listener lines; restarted proxy one Ready pod, new pod NotReady with 2 rejected-listener
lines and no 443 listener, via Service 401 / served.

After every run: the three campaign namespaces are deleted, the 6 Helm releases on the
cluster are unchanged, no LoadBalancer exists, and `~/.kube/config` is unchanged. Each run's
password was random, grepped out of the evidence by the script, and a pattern scan found no
credential, hash or key.

## Earlier attempts (kept, not counted)

- `attempt1-failed/`: one status check failed because it read the users file name from the
  config dump, and Envoy redacts that data source (`[redacted]`). The gateway behaved correctly
  (anonymous 401, valid credentials served, 2 rejected-listener lines). The check now asserts
  basic_auth is active on 443 instead.
- `attempt2-pass-before-probe-hardening/`: passed, before fix 8 and before the restarted-proxy
  record was reordered so its values are not cut at 200 characters.

## Still pending G3 (cannot be verified locally)

| Item | Status |
|---|---|
| Grafana root URL and redirects behind `observe.<domain>` | PENDING_G3 (frozen chart) |
| kagent UI exposes the controller API to the shared credential | PENDING_G3 (owner decision) |
| `ewallet-prompts` ambient mode and network policies | PENDING_G3_PREFLIGHT |
| Gateway API CRDs on the shared GKE cluster | PENDING_G3 (v1.7.0 Standard recommended) |
| The restored GKE context in `~/.kube/config` | PENDING_G3 (not contacted) |
| Real Registry UI answer on `/` (200 or same-host HTTPS redirect) | PENDING_G3 (logic unit-tested) |
| Existing LoadBalancers/Gateways on GKE to approve with `--allow-*` | PENDING_G3 (read-only preflight) |

`SHA256SUMS` covers every file in this directory.
