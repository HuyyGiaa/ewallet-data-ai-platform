# G3-B supervised DNS transition: first live attempt (g3b-dns-transition-20261122T042939Z)

Owner-approved live campaign: `--scope dns-transition`, supervised. Base 72c971fa.
**Result: FAILED (fail-closed) at `private-e2e`; DNS transition NOT_RUN; cleanup COMPLETE; GKE asleep.**

| Item | Result |
|---|---|
| Pre-wake (read-only) | repo clean at 72c971fa = remote; laptop on AC; 15 GiB free RAM; DNS `DNS_OFFLINE_OK` (6 Dynu NS + 1.1.1.1 + 8.8.8.8; PTR `offline.dynu.com`; TTL 120 s); credential files 600 and well-formed; client inside the /32 allowlist; chart sha256 OK; manifest checksums OK (83 workloads); pre-offline AAAA was none (no stored IPv6) |
| Dry run (`--simulate`, real inputs) | expected 46-command order |
| `init` gates (`init-output.json`) | all 9 PASS; `edge_auth_at_init: EDGE_AUTH_MISMATCH` (sync approved) |
| wake | PASS, 5 nodes Ready (04:29:59Z) |
| baseline, temporary-certificate, edge-restricted, L0-render, L0-live | PASS |
| edge-auth | PASS: `EDGE_AUTH_SYNCED` (apply, gateway restart, rollout, gateway check, re-check MATCH) |
| private-e2e | **FAIL**: `public_e2e` 8/8 PASS; `negative_tests` 23/35 |
| open-lb, dns-transition | not run: no LoadBalancer, no Dynu update (`DNS_TARGET_STATE: untouched`) |
| cleanup (04:42:13Z) | acme-responder-off, record-manifest, guarded sleep, verify-sleep: all ok. COMPLETE, SLEEP VERIFIED, 0 nodes |
| after (read-only) | 0 nodes; gateway Service ClusterIP; no forwarding rules; DNS `DNS_OFFLINE_OK` (`dns-after.jsonl`) |

## private-e2e on the real GKE edge

Passed:
- `public_e2e` (`07-private-e2e.jsonl`): certificate verifies and names the 4 hosts; TLS 1.1 refused; Feature API 422 (APPLICATION_REACHED); agents **403** from the non-approved port-forward peer; registry UI, asset and `/v0/agents`.
- `negative_tests` (`07-private-negative.jsonl`):
  - credentials: missing, wrong, unknown user, bearer and identity headers all 401; challenge with realm; valid credentials 422;
  - unauthenticated `features/metrics` 401, `agents` 403;
  - routing: HTTP→HTTPS 301 on the same host; 5 undeclared paths and the wrong method 404;
  - rate limit: `PASS (http2)` 120 requests, 56 served, 64 × 429; anonymous burst 401 only; other route 422; recovery 200.

Failed: the 12 "host … is not served" probes (`unknown`, `a2a`, `fraud`, `vllm`, `mlflow`, `jenkins`, `vault`, `redis`, `mcp`, `kserve`, `prometheus`), all with status 0.

Root cause (test defect, not edge behaviour): `negative_tests.py` used the probed host as SNI. The certificate (self-signed now, Let's Encrypt later) names only the 4 served hosts, so curl's verification failed before any HTTP request. Local kind runs used a certificate that covered those names, so this never showed.

Confirmation (read-only, live, `probe-sni-host-check.txt`, before the nodes slept): SNI `features` with `Host: <a2a|unknown|vault>` gave anonymous **401** and authenticated **404**; control `registry` 200; the old probe 000.

Fix (offline, this commit): `Edge.request` connects to `features.<domain>` for hosts outside `CERTIFICATE_HOSTS` and sends `Host: <host>:<port>`. TLS verification is kept, and plain HTTP is unchanged. There is a new unit test in `tests/ops/test_edge_negative_checks.py`.

## State left on the cluster

- `ewallet-ingress/edge-basic-auth` = `edge-cred`, so the next run's edge-auth should MATCH.
- `ewallet-edge` at rev 3 (campaign values: domain `ewallet-hg.gleeze.com`, source restriction, ACME off); `ewallet-edge-gateway` rev 2, ClusterIP.
- KEDA paused under this campaign. The fresh recovery manifest is in the private evidence (`recovery-manifest/`, not in Git).

Not committed: `l0-edge.yaml` (contains the approved client address), `private/`, `recovery-manifest/`, the self-signed key.
