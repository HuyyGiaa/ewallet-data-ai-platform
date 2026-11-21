# G3-B and G4 evidence plan (one wake for both)

Owner-captured screenshots of the real runtime, saved to `~/Pictures/Screenshots/` as
`GW-NN-<subject>.png`. After the session, run `sha256sum ~/Pictures/Screenshots/GW-*.png` and
record the result in the campaign evidence. Hide credentials:
- terminal: credentials come from `curl -K <private file>`, never typed;
- browser: enter the password in the Basic Auth dialog, which never shows it.

Rubric rows are taken from the owner's brief (rows 37–43); check them against the sheet before the run.

| ID | Rubric | Capture | Expected | What it does not prove |
|---|---|---|---|---|
| GW-01 | 43 | `curl -sS -K cred -X POST -H 'content-type: application/json' -d '<synthetic>' https://features.<D>/v1/features/lookup -w '\n%{http_code}\n'` | 422/424 application JSON over the public HTTPS host | Business lookup success (needs real online data) |
| GW-02 | 43 | Browser padlock / `openssl s_client -connect features.<D>:443 -servername features.<D>` | Trusted chain, SAN `*.<D>` | — |
| GW-03 | 42 | `curl -sS -D - https://features.<D>/v1/features/lookup -X POST -o /dev/null` | `401` + `www-authenticate: Basic realm="E-Wallet"` | — |
| GW-04 | 42 | Browser login dialog, then a page | Page loads after the credential is entered | — |
| GW-05 | 42 | `python3 scripts/edge/negative_tests.py --address <LB-IP> --https-port 443 --http-port 80 --domain <D> --credentials-file cred --rate-limit-mode http2 --out r.jsonl`, then show the burst line | `PASS (http2) … 429=N` with N > 0; recovery 200 | Global limiting (the limit is per gateway pod) |
| GW-06 | 42–43 | `kubectl get gateway,httproute -n ewallet-ingress` + `kubectl get svc ewallet-edge-gateway -n ewallet-ingress` | Programmed/Accepted; LoadBalancer with source ranges | — |
| GW-07 | 40 | `https://agents.<D>/` from an allowed address: open an agent and run one short, synthetic test prompt | Agent answer in the kagent UI | A denial demo: also show 403 from a non-allowed address (GW-12) |
| GW-08 | 41 | `https://registry.<D>/` | Registry UI listing agents/prompts | — |
| GW-09 | 37 | `https://observe.<D>/` → a metrics dashboard with live panels | Non-empty Prometheus panels | A healthy datasource alone is not metrics evidence |
| GW-10 | 38 | Grafana Explore → Loki, a query on a platform workload | Recent log lines | Healthy Loki alone is not logs evidence |
| GW-11 | 39 | Grafana Explore → Tempo, a recent trace | A trace with spans | Healthy Tempo alone is not traces evidence |
| GW-12 | support | Non-allowed network: `curl -m 10 https://agents.<D>/` → timeout (LB firewall); optional L5 test → 403 | Restriction enforced | — |
| GW-13 | support | `kubectl get svc -A \| grep LoadBalancer` + DNS `dig +short agents.<D>` | One LB, records resolve to it | — |
| GW-14 | support | Harness lines "host vllm/mlflow/… is not served" | 401/404, never a backend | — |
| GW-15 | support | `cloud_lab.py verify --expect sleep` after rollback to ClusterIP | `failures: []`, 0 nodes, no LB | — |

Order within the wake:
1. GW-02, 03, 04, 01, 05, 06, 13: gateway and Feature API.
2. GW-07, 08: UIs.
3. GW-09, 10, 11: Grafana. Generate a little traffic first with the GW-01 calls so fresh logs and traces exist.
4. GW-12, 14: security.
5. Rollback, sleep, then GW-15.

GW-07 runs one agent prompt, which uses the LLM. Keep it to a single synthetic prompt; no PII.
