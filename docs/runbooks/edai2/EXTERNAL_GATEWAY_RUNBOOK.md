# External gateway: public deployment (G3-B) and evidence replay (G4)

The replayable procedure for publishing the four edge routes on
`ewallet-hg.gleeze.com` and collecting the rubric evidence (rows 37–43). Every check is a
repository command run from the repository root. The architecture (Istio 1.30.7, Gateway API
v1.7.0, `ewallet-edge` + `ewallet-edge-gateway` in `ewallet-ingress`) is unchanged from G3-A1.
This runbook supersedes `G3B_DEPLOYMENT_PLAN.md` in the G3-B0 and G3-B1 evidence folders.

**Nothing here runs without the owner's approval of the campaign.** Steps marked
**[owner]** are done by the owner (Dynu, screenshots).

## Inputs

```bash
D=ewallet-hg.gleeze.com
CTX=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
KCF=~/.cache/ewallet-cloud-lab/kubeconfig
K="--kubeconfig $KCF --context $CTX"
CID=g3b-<UTC timestamp>                        # one campaign id for every step
EV=~/ewallet-evidence/gateway/$CID             # private evidence, mode 700
PRIV=$EV/private                               # credentials, keys, owner values; never in Git
M0=~/ewallet-evidence/gateway/g3a1-20261121T175830Z/11-sleep/recovery-manifest
```

Owner-supplied files in `$PRIV`; the scripts refuse paths inside the repository:

| File | Content |
|---|---|
| `approved-cidrs.txt` | the public IPv4 /32–/16 addresses that may use `agents.$D`, one per line |
| `edge-cred` | edge Basic Auth `user:password` (`create_basic_auth.sh` reads the password on stdin) |
| `grafana-cred` | Grafana admin `user:password` (read from its Secret into the file, never printed) |
| `g3b-values.yaml` | `domain: $D`, `sourceRestriction: {enabled: true, allowedCidrs: [...same as the file...]}`, `acmeHttp01: {enabled: true}` on top of `g3a-private-values.yaml` |

## Procedure

Evidence classes are never mixed: `LOCAL_KIND`, `GKE_PRIVATE` (port-forward) and
`PUBLIC_INTERNET` (from outside the cluster).

| # | Step | Command | Gate |
|---|---|---|---|
| P1 | Sleep feasibility, before any cost | `python3 scripts/edge/platform_checks.py $K sleep-feasibility --manifest $M0 [--also-approve ewallet-mcp/feature-rag-mcp]` | every predicted blocker approved; the extra PDB needs an owner approval for **this** campaign |
| P2 | DNS before | `python3 scripts/edge/dns_check.py --domain $D --expect-offline` | records the starting shape (values withheld) |
| 1 | Wake | `CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest $M0 --nodes 5 --execute --confirm-cluster ewallet-dev` | nodes Ready |
| 2 | Private baseline | `python3 scripts/edge/platform_checks.py $K backends --snapshot-out $EV/baseline.json --out $EV/02-backends.jsonl`; `… gateway`; `… envoy` | all PASS (G3-A1 state) |
| 3 | Temporary certificate for the new names | `scripts/edge/issue_certificate.sh --environment self-signed --domain $D --context $CTX --kubeconfig $KCF --private-dir $PRIV/tls --import` | 4 SANs; Secret replaced |
| 4 | Edge with domain, restriction, responder (still ClusterIP) | `helm upgrade ewallet-edge infra/helm/ewallet-edge -n ewallet-ingress --kubeconfig $KCF --kube-context $CTX -f docs/evidence/edai2/gateway/g3a0_closure_20261121T172654Z/helm/g3a-private-values.yaml -f $PRIV/g3b-values.yaml` | `platform_checks.py $K gateway`, `… envoy --restricted`, `… watch` (kagent-ui Ready 180 s) |
| 5 | **L0 live** | `python3 scripts/edge/ownership_preflight.py live --context $CTX --kubeconfig $KCF --allow-public-loadbalancer --domain $D --approved-cidrs-file $PRIV/approved-cidrs.txt` | `EDGE_OWNERSHIP_OK`: policy selects the gateway pods, DENY on agents.$D (+`:*`) port 443, CIDRs = approved; Basic Auth, rate limit and RBAC active in every pod |
| 6 | **L0 render** | `helm template ewallet-edge-gateway <istio/gateway 1.30.7> -f infra/helm/ewallet-edge/values-gateway.yaml -f …/values-gateway-public.yaml -f …/values-gateway-public-open.yaml > $EV/gw.yaml`; `helm template ewallet-edge … -f g3a-private-values.yaml -f $PRIV/g3b-values.yaml > $EV/edge.yaml`; `python3 scripts/edge/ownership_preflight.py render --allow-public-loadbalancer --domain $D --approved-cidrs-file $PRIV/approved-cidrs.txt $EV/gw.yaml $EV/edge.yaml` | `EDGE_OWNERSHIP_OK`. **If L0 fails, the LB is not opened** |
| 7 | Private E2E over port-forward | `kubectl $K -n ewallet-ingress port-forward --address 127.0.0.1 svc/ewallet-edge-gateway 18443:443 18080:80`; `python3 scripts/edge/public_e2e.py --address 127.0.0.1 --https-port 18443 --http-port 18080 --domain $D --ca-file $PRIV/tls/self-signed/fullchain.pem --credentials-file $PRIV/edge-cred --agents denied --evidence-class GKE_PRIVATE --out $EV/07-e2e.jsonl` | agents 403 (the port-forward peer is not allowed); other rows PASS |
| 8 | Open the LB | `helm upgrade ewallet-edge-gateway <chart> -n ewallet-ingress … -f values-gateway.yaml -f values-gateway-public.yaml -f values-gateway-public-open.yaml`, then step 5 again | one LB, ports 80/443, `Local`; L0 live OK |
| 9 | **[owner]** DNS online | Dynu: set the IPv4 of `$D` to the LB IP (wildcard alias covers the names; no IPv6). Then `python3 scripts/edge/dns_check.py --domain $D --expect-ip <LB IP>` until `DNS_ONLINE_OK` | authoritative + 1.1.1.1 + 8.8.8.8, 4 names + wildcard, no AAAA |
| 10 | Internet gates before the real certificate | `python3 scripts/edge/negative_tests.py --address <LB IP> --domain $D --ca-file $PRIV/tls/self-signed/fullchain.pem --credentials-file $PRIV/edge-cred --rate-limit-mode http2 --out $EV/10-negative.jsonl` (L2, L3, L6); `public_e2e.py … --address <LB IP> --agents allowed --evidence-class PUBLIC_INTERNET` from an approved address (L5); `public_e2e.py … --agents denied` from a non-approved network (L4) | all PASS, else step 15 |
| 11 | Staging certificate | `scripts/edge/issue_certificate.sh --environment staging --domain $D --context $CTX --kubeconfig $KCF --private-dir $PRIV/le` | 4 SANs, no challenge left |
| 12 | Production certificate | `scripts/edge/issue_certificate.sh --environment production --confirm-production --import --domain $D --context $CTX --kubeconfig $KCF --private-dir $PRIV/le` | L1: `public_e2e.py` without `--ca-file` (system trust) |
| 13 | Responder off; Grafana root URL | `helm upgrade ewallet-edge …` with `acmeHttp01.enabled=false`. Grafana: `release_diff.py` twice (see its header): live vs current must be `RELEASE_DIFF_OK`, current vs proposed `--allow-grafana-root-url https://observe.$D/` must be `RELEASE_DIFF_OK`; only then `helm upgrade llm-agent … -f <live values> --set grafanaExternal.rootUrl=https://observe.$D/` | L9 in step 14 |
| 14 | G4 evidence | the table below, `--evidence-class PUBLIC_INTERNET`; `platform_checks.py $K compare --baseline $EV/baseline.json` (L8); L0 live again (L7) | all PASS |
| 15 | **[owner]** DNS offline, then close | see *DNS cleanup*; then `helm upgrade ewallet-edge-gateway … -f values-gateway.yaml` (ClusterIP), `ownership_preflight.py live --context $CTX --kubeconfig $KCF` (no LB) | `DNS_OFFLINE_OK`, `EDGE_OWNERSHIP_OK` |
| 16 | Guarded sleep | `cloud_lab.py record-manifest --out $EV/recovery-manifest --campaign $CID …`; `cloud_lab.py plan-sleep --manifest docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z`; `CLOUD_LAB_APPROVED=yes CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:$CID CLOUD_LAB_KEDA_PAUSE_APPROVAL=ewallet-dev:$CID cloud_lab.py sleep --scale-pdb-guarded --pause-keda --manifest docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z --campaign $CID [--also-approve <only what plan-sleep lists and the owner approved>] --execute --confirm-cluster ewallet-dev`; `cloud_lab.py verify --expect sleep --manifest $EV/recovery-manifest` | `failures: []`, 0 nodes |

On any failed gate: step 15, then step 16; the investigation happens privately.

## DNS cleanup

Goals:
- never point the names back to the owner's home network;
- never leave them on an LB address that GKE is about to release;
- keep the Dynu hostname for later demos.

1. Stop the public tests.
2. **[owner]** In Dynu, take `ewallet-hg.gleeze.com` offline in a way the interface actually supports. Preferred: *Offline Settings* (hostname offline, no offline IP), or remove/disable the IPv4 address so the wildcard alias returns nothing. If Dynu only offers an offline IP, choosing a placeholder (for example a documentation address) is an owner decision, and the check takes it via `--offline-ip`. No other workaround is used.
3. `python3 scripts/edge/dns_check.py --domain $D --expect-offline [--offline-ip <placeholder>]`, repeated until `DNS_OFFLINE_OK`. The TTL is about 120 s; allow up to 10 minutes for public resolvers.
4. Only then release the LB (step 15), and verify with `ownership_preflight.py live`.

Checklist recorded in the campaign evidence. Values that are not the LB address are written as "withheld".

```text
DNS hostname:                 ewallet-hg.gleeze.com
Wildcard A:                   (dns_check: present / none)
Wildcard AAAA:                (must be none)
Current target:               LB IP | offline | withheld
Planned offline state:        Dynu Offline Settings / IPv4 removed / owner placeholder
Authoritative DNS result:     dns_check lines for ns1/ns3/ns4.dynu.com
Public resolver result:       dns_check lines for 1.1.1.1 and 8.8.8.8
LB released:                  ownership_preflight live: no LoadBalancer
Final DNS state:              DNS_OFFLINE_OK
```

## G4 evidence replay (rows 37–43)

Every row is a repository command. `public_e2e.py` prints one JSON line per check with the
row number and the evidence class; `negative_tests.py` writes JSONL. Each row also has an
owner screenshot taken during the same session.

| Row | Script and invocation | Expected | Owner screenshot |
|---|---|---|---|
| 37 Metrics UI | `python3 scripts/edge/public_e2e.py --address <LB IP> --domain $D --credentials-file $PRIV/edge-cred --grafana-credentials-file $PRIV/grafana-cred --grafana-root-url https://observe.$D/ --evidence-class PUBLIC_INTERNET --out $EV/14-e2e.jsonl` (row 37 lines) | redirect to login on the same host, appUrl = public URL, login, dashboards > 0, Prometheus `up` series > 0 | `https://observe.$D/` → a metrics dashboard with live panels |
| 38 Logs UI | same run (row 38) | Loki returns log lines for `{namespace="ewallet-agents"}` | Grafana Explore → Loki query with recent lines |
| 39 Traces UI | same run (row 39) | Tempo returns traces | Grafana Explore → Tempo, one trace with spans |
| 40 Agent Test UI | same run (row 40) from an approved address; `--agents denied` from another network | UI, static asset, read-only `/api/agents`; 403 elsewhere | `https://agents.$D/`, one short synthetic test prompt answered (uses the LLM once; no PII) |
| 41 Agent Registry UI | same run (row 41) | UI, static asset, `/v0/agents` 200 | `https://registry.$D/` listing |
| 42 Basic Auth & rate limiting | `python3 scripts/edge/negative_tests.py --address <LB IP> --domain $D --credentials-file $PRIV/edge-cred --rate-limit-mode http2 --out $EV/14-negative.jsonl` | 401 + challenge for missing, wrong, forged; valid → app; burst verdict `PASS (http2)` with 429s; recovery; other route served | browser Basic Auth dialog; terminal showing the 401 headers and the burst line |
| 43 Domain + HTTPS (Feature API) | same `public_e2e.py` run (row 43): certificate verifies with system trust, 4 SANs, TLS 1.1 refused, Feature API 422 + correlation_id (`--lookup-json` for a synthetic business lookup) | as stated; a 422/424 is APPLICATION_REACHED, not a business success | browser padlock on `https://features.$D/`; terminal with the lookup response |

Screenshots go to `~/Pictures/Screenshots/GW-<row>-<subject>.png`. Record
`sha256sum ~/Pictures/Screenshots/GW-*.png` in the campaign evidence. Credentials never
appear: they are entered in the browser dialog or come from `-K`/`--credentials-file`.

## Local reproduction (no cloud)

| What | Command |
|---|---|
| Edge security, KA, ACME HTTP-01 through the gateway with Pebble (`issue_certificate.sh`) | `EDGE_ACME_TEST=1 scripts/edge/local_validate.sh <dir> full` (kind) |
| Staged 0 → 1 rollout | `scripts/edge/staged_rollout_check.sh <dir>` (kind) |
| Grafana root URL against the pinned image | `scripts/edge/grafana_root_url_check.sh https://observe.edge.example.test/` (Docker) |
| Unit tests | `python3 -m pytest tests/ops/test_public_preflight.py tests/ops/test_edge_scripts.py tests/ops/test_edge_negative_checks.py tests/ops/test_session_a_charts.py` |
