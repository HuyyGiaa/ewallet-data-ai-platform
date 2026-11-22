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
| `edge-cred` | edge Basic Auth: exactly one line `user:password`, password ≥ 24 characters, mode 600. `create_basic_auth.sh --credentials-file` checks or writes the `edge-basic-auth` Secret from it |
| `dynu.json` | Dynu IP update credentials (mode 600; only metadata is ever checked) |
| `grafana-cred` | Grafana admin `user:password` (read from its Secret into the file, never printed) |
| `g3b-values.yaml` | `domain: $D`, `sourceRestriction: {enabled: true, allowedCidrs: [...same as the file...]}`, `acmeHttp01: {enabled: true}` on top of `g3a-private-values.yaml` |

## Procedure

Evidence classes are never mixed: `LOCAL_KIND`, `GKE_PRIVATE` (port-forward) and
`PUBLIC_INTERNET` (from outside the cluster).

| # | Step | Command | Gate |
|---|---|---|---|
| P1 | Sleep feasibility, before any cost | `python3 scripts/edge/platform_checks.py $K sleep-feasibility --manifest $M0 [--also-approve ewallet-mcp/feature-rag-mcp]` | every predicted blocker approved; the extra PDB needs an owner approval for **this** campaign |
| P2 | DNS before | `python3 scripts/edge/dns_check.py --domain $D --expect-offline` | records the starting shape (values withheld); exit 2 = `DNS_CHECK_FAILED` (state not provable) |
| 1 | Wake | `CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest $M0 --nodes 5 --execute --confirm-cluster ewallet-dev` | nodes Ready |
| 2 | Private baseline | `python3 scripts/edge/platform_checks.py $K backends --snapshot-out $EV/baseline.json --out $EV/02-backends.jsonl`; `… gateway`; `… envoy` | all PASS (G3-A1 state) |
| 3 | Temporary certificate for the new names | `scripts/edge/issue_certificate.sh --environment self-signed --domain $D --context $CTX --kubeconfig $KCF --private-dir $PRIV/tls --import` | 4 SANs; Secret replaced |
| 4 | Edge with domain, restriction, responder (still ClusterIP) | `helm upgrade ewallet-edge infra/helm/ewallet-edge -n ewallet-ingress --kubeconfig $KCF --kube-context $CTX -f docs/evidence/edai2/gateway/g3a0_closure_20261121T172654Z/helm/g3a-private-values.yaml -f $PRIV/g3b-values.yaml` | `platform_checks.py $K gateway`, `… envoy --restricted`, `… watch` (kagent-ui Ready 180 s) |
| 5 | **L0 live** | `python3 scripts/edge/ownership_preflight.py live --context $CTX --kubeconfig $KCF --allow-public-loadbalancer --domain $D --approved-cidrs-file $PRIV/approved-cidrs.txt` | `EDGE_OWNERSHIP_OK`: policy selects the gateway pods, DENY on agents.$D (+`:*`) port 443, CIDRs = approved; Basic Auth, rate limit and RBAC active in every pod |
| 6 | **L0 render** | `helm template ewallet-edge-gateway <istio/gateway 1.30.7> -f infra/helm/ewallet-edge/values-gateway.yaml -f …/values-gateway-public.yaml -f …/values-gateway-public-open.yaml > $EV/gw.yaml`; `helm template ewallet-edge … -f g3a-private-values.yaml -f $PRIV/g3b-values.yaml > $EV/edge.yaml`; `python3 scripts/edge/ownership_preflight.py render --allow-public-loadbalancer --domain $D --approved-cidrs-file $PRIV/approved-cidrs.txt $EV/gw.yaml $EV/edge.yaml` | `EDGE_OWNERSHIP_OK`. **If L0 fails, the LB is not opened** |
| 6a | Edge Basic Auth Secret = `edge-cred` | `bash scripts/edge/create_basic_auth.sh --credentials-file $PRIV/edge-cred --check --kubeconfig $KCF --context $CTX` (read-only; prints only `EDGE_AUTH_MATCH` / `EDGE_AUTH_MISMATCH` / `EDGE_AUTH_MISSING` / `EDGE_AUTH_CHECK_FAILED`). On a mismatch, **with the owner's approval** only: `… --apply`, then `kubectl $K -n ewallet-ingress rollout restart deployment/ewallet-edge-gateway`, `… rollout status … --timeout=300s`, `--check` again | `EDGE_AUTH_MATCH`; Envoy reads the users file at start-up, so the restart is part of the step. Step 7 proves the behaviour; **a mismatch or a failing step 7 means no LB** |
| 7 | Private E2E over port-forward | `kubectl $K -n ewallet-ingress port-forward --address 127.0.0.1 svc/ewallet-edge-gateway 18443:443 18080:80`; `python3 scripts/edge/public_e2e.py --address 127.0.0.1 --https-port 18443 --http-port 18080 --domain $D --ca-file $PRIV/tls/self-signed/fullchain.pem --credentials-file $PRIV/edge-cred --agents denied --evidence-class GKE_PRIVATE --out $EV/07-e2e.jsonl`; `python3 scripts/edge/negative_tests.py --address 127.0.0.1 --https-port 18443 --http-port 18080 --domain $D --ca-file … --credentials-file $PRIV/edge-cred --rate-limit-mode http2` | valid credentials accepted on every allowed route; missing, wrong, unknown-user, bearer and forged-header requests 401 (agents: 401 or 403); agents 403 with valid credentials (the port-forward peer is not allowed); burst limited (429) |
| 8 | Open the LB | `helm upgrade ewallet-edge-gateway <chart> -n ewallet-ingress … -f values-gateway.yaml -f values-gateway-public.yaml -f values-gateway-public-open.yaml`, then step 5 again | one LB, ports 80/443, `Local`; L0 live OK |
| 9 | DNS online | `python3 scripts/edge/dynu_dns.py --credentials $PRIV/dynu.json --execute online --ip <LB IP>` (`myip=<LB>&myipv6=no`; the wildcard alias covers the names). Only after the first transition was proven (*First DNS transition*). Then `python3 scripts/edge/dns_check.py --domain $D --expect-ip <LB IP>` until `DNS_ONLINE_OK` | authoritative + 1.1.1.1 + 8.8.8.8, 4 names + wildcard, no AAAA |
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
2. Take the hostname offline: `dynu_dns.py --execute offline --mode signoff --offline-ip $OFF4 --offline-ipv6 $OFF6 --released-ip <LB IP>` once API signoff is verified (see *Dynu offline: dual-stack*), else the panel's *Offline Settings → Go Offline Now*. Dynu's offline state answers its offline server on A **and** AAAA. `myip=no` is not an offline method. No other workaround is used.
3. `python3 scripts/edge/dns_check.py --domain $D --expect-offline --offline-ip $OFF4 --offline-ipv6 $OFF6 --released-ip <LB IP>`, repeated until `DNS_OFFLINE_OK`. The TTL is about 120 s; allow up to 10 minutes for public resolvers.
4. Only then release the LB (step 15), and verify with `ownership_preflight.py live`.

Checklist recorded in the campaign evidence. Values that are not the LB address are written as "withheld".

```text
DNS hostname:                 ewallet-hg.gleeze.com
Wildcard A:                   (dns_check: present / none)
Wildcard AAAA:                online: none; offline: none or OFF6
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

## Supervised G3-B campaign (the approved way to run G3-B)

The owner is present; Claude runs the technical steps through `scripts/edge/g3b_campaign.py`.
The orchestrator runs the procedure above as phases with every gate, then always cleans up and
sleeps.

### Stages and their approvals

| Stage | What | Command | Cloud / DNS change | Owner approval |
|---|---|---|---|---|
| 0. Offline preparation | private files (mode 600), offline addresses re-read, `init` (read-only: render, Secret check, DNS check, client address) | `g3b_campaign.py init …` | none | none |
| 1. Private deployment and checks | wake, baseline, temporary certificate, edge, L0, **edge-auth**, **private-e2e** | first phases of either scope | wake; edge upgrade; Secret sync if mismatched | the scope's approval, plus `--approve-edge-secret-sync` when the Secret differs |
| 2. DNS transition verification (once) | LB opened on the checked edge, Offline → Online (LB) → signoff → Offline, then cleanup | `init --scope dns-transition` + `run` | LB (released at the end), two Dynu updates | `--approve-dns-transition-test` (not the G3-B live approval) |
| 3. Public deployment (G3-B) | LB, DNS online, Internet gates, L4, certificates, Grafana, public E2E | `init` (scope `g3b`) + `run` | everything in the procedure | `--approve-live-campaign` + the gated flags; `--dns-offline-verified` with stage 2's evidence |
| 4. Cleanup | ACME off, DNS offline proven, LB released, guarded sleep, verify | automatic; `g3b_campaign.py cleanup --state …` to resume | as listed | covered by the scope's approval |
| 5. G4 evidence replay | owner session with screenshots | *Owner G4 replay* | owner-controlled wake | separate |

### First DNS transition (`--scope dns-transition`)

Why: `offline=yes` was tested by the owner on 2026-11-22 while the hostname was **already**
Offline (`good`, `DNS_OFFLINE_OK`), so it proves nothing about Online → Offline. Dynu's protocol
page documents `offline=yes` only. It does not say whether a later `myip=` update brings the
hostname back online, and it documents no `offline=no`. The only address that may be published
for the test is one the owner controls: the GKE LB of this campaign. A home address, a
third-party address or a documentation address must never be published.

Order (each step proven by `dns_check.py` at every Dynu NS plus 1.1.1.1 and 8.8.8.8, never by
Dynu's answer word):
1. Stage 1 phases unchanged: wake, baseline, temporary certificate, edge, L0 render, L0 live, edge-auth, private-e2e. The LB opens only after the private security checks.
2. `open-lb` + L0 live on the public service.
3. `dns-transition`:
   1. **pre-check**: `--expect-offline` with OFF4/OFF6 must be `DNS_OFFLINE_OK`, else no update is sent (`DNS_TRANSITION_NOT_STARTED`);
   2. **online**: `dynu_dns.py --execute online --ip <LB>`, then `DNS_ONLINE_OK` (A = LB everywhere, no AAAA), else `DNS_TRANSITION_ONLINE_NOT_PROVEN`;
   3. **signoff**: `dynu_dns.py --execute offline --mode signoff --offline-ip OFF4 --offline-ipv6 OFF6 --released-ip <LB>`, then `DNS_OFFLINE_OK`, else `DNS_TRANSITION_OFFLINE_NOT_PROVEN`;
   4. `DNS_TRANSITION=VERIFIED` in `state.json`; outputs are kept in `dns-transition-{0-precheck,1-online,2-offline}.jsonl`.
4. Cleanup as always. The LB is released only when DNS is proven offline; then guarded sleep.

No certificate, no Internet gate, no G3-B or G4 verdict (`NOT_RUN (dns-transition scope)`).

```bash
CID=g3b-dns-transition-<UTC>; EV=~/ewallet-evidence/gateway/$CID
python3 scripts/edge/g3b_campaign.py init --scope dns-transition --mode supervised --campaign $CID --campaign-dir $EV \
  --manifest $M0 --approved-cidrs $PRIV/approved-cidrs.txt --edge-credentials $PRIV/edge-cred \
  --dns-credentials $PRIV/dynu.json --dns-offline-mode signoff --dns-offline-ip $OFF4 --dns-offline-ipv6 $OFF6 \
  --approve-dns-transition-test [--approve-edge-secret-sync] --approve-guarded-sleep --approve-keda-pause \
  --also-approve ewallet-mcp/feature-rag-mcp
systemd-inhibit --what=sleep:idle python3 scripts/edge/g3b_campaign.py run --state $EV/state.json
```

Gates (`TRANSITION_GATES`): manifest, PDB, KEDA, guarded sleep, client address, L0 render,
`EDGE_AUTH_READY`, `DNS_TRANSITION_TEST_READY` (supervised, signoff with both offline addresses,
DNS credentials, hostname proven offline **now**), `OWNER_DNS_TRANSITION_APPROVAL`. The G3-B live
approval neither replaces nor is replaced by it.

| Outcome | DNS | LB | Next |
|---|---|---|---|
| `VERIFIED` | offline (proven) | released | stage 3 with `--dns-offline-verified --dns-transition-evidence $EV/state.json` |
| `ONLINE_NOT_PROVEN` (Dynu stays Offline after `myip=`) | cleanup sends signoff and proves offline | released once proven | owner decision: bring it online in the panel during stage 3 (a supervised manual step), or stop |
| `OFFLINE_NOT_PROVEN` | names may still point at the LB | **kept** (still ours, nothing dangles); nodes still sleep; `CLEANUP_INCOMPLETE` | owner: panel *Go Offline Now*, then `g3b_campaign.py cleanup --state $EV/state.json` (proves offline, releases the LB). Signoff is not used for stage 3 |
| `NOT_STARTED` (pre-check not `DNS_OFFLINE_OK`; no Dynu update sent) | untouched | released only if cleanup's fresh `dns_check` proves offline; otherwise **kept**, `CLEANUP_INCOMPLETE` | owner fixes DNS (panel *Go Offline Now*), `cleanup` again; re-read OFF4/OFF6, re-run `init` |

Observed, first live attempt `g3b-dns-transition-20261122T042939Z` (2026-11-22):
- All gates and phases passed up to `private-e2e`: wake (5 nodes), baseline, self-signed certificate, edge, L0 render/live, and edge-auth (Secret synced, gateway restarted, re-check MATCH).
- In `private-e2e`, the 8 `public_e2e` rows passed (TLS, Feature API 422, agents 403, registry), and so did 23 of 35 `negative_tests` checks (Basic Auth, routing, HTTP/2 burst with 64 × 429).
- The 12 "host … is not served" probes failed with status 0. They used the probed host as SNI, so curl rejected the 4-name certificate before any HTTP answer.
- As designed, no LB was opened and no Dynu update was sent; cleanup COMPLETE, sleep VERIFIED, 0 nodes.
- A read-only check on the live gateway confirmed the corrected probe (SNI `features`, `Host: <host>`): anonymous 401, authenticated 404. It is now in `negative_tests.py`.
- Left on the cluster: `edge-basic-auth` matches `edge-cred`; `ewallet-edge` is at rev 3 (campaign values, ACME off, ClusterIP).

Observed, live retry `g3b-dns-transition-retry-20261122T050543Z` (2026-11-22):
- Private security passed in full: `public_e2e` 8/8 and `negative_tests` 35/35 (unserved hosts: anonymous 401, authenticated 404). Edge auth MATCH, no mutation. The LB opened.
- Pre-check `DNS_OFFLINE_OK`. Then `online --ip <LB>` (`myip=<LB>&myipv6=no`): Dynu answered `good`, and **A switched to the LB on every server** after about 2.5 minutes (one TTL). So an API update does bring the hostname out of Offline.
- **AAAA stayed on Dynu's offline IPv6 (OFF6)** for all 10 attempts (5 minutes). ns1 was read directly while still Online: A = LB, AAAA = OFF6. Result: `DNS_TRANSITION_ONLINE_NOT_PROVEN`, because the online policy requires no AAAA.
- Cleanup sent signoff from that real Online state and proved `DNS_OFFLINE_OK` with `--released-ip <LB>`. The fresh check passed, the LB was released, sleep was VERIFIED with 0 nodes, and no forwarding rule or address remained.
- Open (owner decision before the next attempt): how to bring AAAA online. Either clear or disable the hostname's IPv6 in the Dynu panel, or accept AAAA = OFF6 as a documented online state (IPv6 clients would then reach Dynu's offline page, not the edge). Sending no `myipv6` could publish the updater's own IPv6 (home), so it is not an option.

`--dns-offline-verified` alone does nothing. Stage 3's `SAFE_CLEANUP_READY` needs
`--dns-transition-evidence` pointing at the `state.json` of a **real** transition run. That file must show:
- `scope: dns-transition`, `simulated: false` (`--simulate` runs are marked and always refused);
- `DNS_TRANSITION: VERIFIED` and the `dns-transition` phase `PASS`;
- `CLEANUP_STATUS: COMPLETE`, `SLEEP_STATUS: VERIFIED`, `LAST_VERIFIED_NODE_COUNT: 0`;
- cleanup steps `dns-verify-before-release` and `lb-release` ok (the LB was released after DNS was proven offline);
- the same domain, offline mode, OFF4 and OFF6.

A transition whose cleanup is incomplete (LB kept, sleep not verified) is not evidence; finish
it with `cleanup`, after which its `state.json` qualifies.

### Preconditions and owner approvals

| Input | Where |
|---|---|
| Approved client CIDRs (must include this machine's current public address, for L5) | `$PRIV/approved-cidrs.txt` |
| Edge and Grafana credentials | `$PRIV/edge-cred`, `$PRIV/grafana-cred` (Grafana: needed for rows 37–39; without it Grafana is BLOCKED, never PASS) |
| Dynu IP update credentials; approved offline IPv4/IPv6; the first transition proven (*First DNS transition*) | `$PRIV/dynu.json`, `--dns-offline-mode signoff --dns-offline-ip … --dns-offline-ipv6 …`, `--dns-offline-verified --dns-transition-evidence <stage 2 state.json>` |
| Edge Secret differs from `edge-cred` (`init` reports `edge_auth_at_init`) | `--approve-edge-secret-sync` (a Kubernetes mutation of `ewallet-ingress/edge-basic-auth`; the old password stops working) |
| Recovery manifest | `$M0` (G3-A1, checksums OK) |
| PDB exception for this campaign | `--also-approve ewallet-mcp/feature-rag-mcp` |
| Guarded sleep, KEDA pause, production certificate, Grafana root URL | `--approve-guarded-sleep --approve-keda-pause --approve-production-certificate [--approve-grafana-root-url]` |
| The live run itself | `--approve-live-campaign` |

```bash
python3 scripts/edge/g3b_campaign.py init --mode supervised --campaign $CID --campaign-dir $EV --manifest $M0 \
  --approved-cidrs $PRIV/approved-cidrs.txt --edge-credentials $PRIV/edge-cred --grafana-credentials $PRIV/grafana-cred \
  --dns-credentials $PRIV/dynu.json --dns-offline-mode signoff --dns-offline-ip <OFF4> --dns-offline-ipv6 <OFF6> \
  --dns-offline-verified --dns-transition-evidence <stage 2 state.json> --approve-live-campaign --approve-guarded-sleep --approve-keda-pause --also-approve ewallet-mcp/feature-rag-mcp \
  --approve-production-certificate --approve-grafana-root-url
```

`init` must print every gate as PASS:
- `CLOUD_LAB_RECOVERY_MANIFEST_VALID`, `PDB_APPROVAL_COMPLETE`, `KEDA_SLEEP_APPROVAL_READY`, `GUARDED_SLEEP_PLAN_VALID`;
- `CLIENT_ADDRESS_APPROVED`;
- `SAFE_CLEANUP_READY` (DNS automation);
- `L0_RENDER_READY` (the exact public render passes the strict preflight, offline);
- `EDGE_AUTH_READY` (Secret = `edge-cred`, or a mismatch with `--approve-edge-secret-sync`);
- `OWNER_LIVE_APPROVAL`.

Supervised mode does not require the watchdog, linger or laptop-risk acceptance. Everything
that protects security and data is still required.

### Run, watch, stop

```bash
systemd-inhibit --what=sleep:idle python3 scripts/edge/g3b_campaign.py run --state $EV/state.json
python3 scripts/edge/g3b_campaign.py status --state $EV/state.json      # in another terminal
```

| Behaviour | What happens |
|---|---|
| Deadline | 3 h from the wake; no step starts after it |
| Commands | each runs in its own process group with a timeout bounded by the deadline (max 30 min); a timeout ends the whole group |
| Stop safely | Ctrl-C or `kill -TERM <main pid>` (shown in `status`): the running command is ended, then cleanup runs. A second signal does not interrupt cleanup |
| Main process hung | `kill -9 <main pid>`, then `g3b_campaign.py cleanup --state $EV/state.json` (resumable). The optional watchdog does the same after a stale heartbeat, and sends SIGTERM to a main process still running 15 min past the deadline. It never cleans up while the main process holds the campaign lock |

### Gates and where they run

| Gate | Phase | Evidence class | Notes |
|---|---|---|---|
| L0 render | `init` and `L0-render` | LOCAL (render) | exact public values; `ownership_preflight.py render --allow-public-loadbalancer` |
| L0 live | `L0-live` | GKE_PRIVATE | active Envoy filters, RBAC CIDRs = approved; **if L0 fails, no LB** |
| Edge Basic Auth | `edge-auth` | GKE_PRIVATE | `create_basic_auth.sh --check`; on an approved mismatch `--apply`, gateway restart, rollout status, `platform_checks gateway`, `--check`. Nothing secret is printed or stored. **A mismatch without approval, or a sync that does not take, means no LB** |
| Private E2E | `private-e2e` | GKE_PRIVATE | port-forward; Feature API, Registry, Grafana (if credentials), filters; agents must be **403** (the port-forward peer is never approved); `negative_tests.py`: missing/wrong/forged credentials 401, valid accepted, rate limit 429. A gateway still holding the old users file fails here. It is not L4. **If it fails, no LB** |
| L2/L3/L6 | `internet-gates` | PUBLIC_INTERNET | `negative_tests.py --rate-limit-mode http2` |
| L5 | `internet-gates` | PUBLIC_INTERNET | this machine must be inside the approved CIDRs (checked first) |
| L4 | `L4-denied` | PUBLIC_INTERNET, **only from a non-approved address** | this laptop on another network; otherwise `L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE` |
| L1 | `production-certificate` + `public-e2e` (row 43) | PUBLIC_INTERNET | system trust, 4 SANs, TLS 1.1 refused |
| L7/L8 | `public-e2e` | GKE_PRIVATE / PUBLIC_INTERNET | ownership preflight live; regression against the baseline |
| L9 / rows 37–39 | `grafana` + `public-e2e` | PUBLIC_INTERNET | `release_diff.py` must be clean before the one-line upgrade; rows 37–39 must pass, else `GRAFANA_VERIFICATION_*` |

**L4 needs a real request from outside the approved CIDRs.** No mocks, forged headers,
port-forward, kind run or result file from elsewhere counts. When `status` shows
`CURRENT_PHASE: L4-denied`, the phase waits up to 20 minutes for the owner to move **this
laptop** to another network (for example a phone hotspot). It then:
1. confirms the public address is outside the approved CIDRs (the address is not recorded);
2. deletes any old result and sends a real request with valid credentials to the public LB (`public_e2e.py --agents denied`);
3. requires 403 for the agents host in the result this request wrote;
4. confirms the address is still outside the approved CIDRs.

Then the owner switches back. The client-side address check only decides whether the test may
run; the acceptance is the gateway's 403 to that real request.

`l4-external.jsonl` from a second machine is **not accepted**: nothing in such a file proves its
source address, its age or its campaign. Without a verified source, L4 is
`L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE`. The campaign continues (nothing unsafe), but the
security verdict cannot be complete. A failing L4 (agents reachable from a non-approved
address) stops the campaign and cleans up.

### Verdicts

`status` and the end of `run` print:
- `G3B_DEPLOYMENT_STATUS`, `G3B_SECURITY_STATUS`, `G3B_PUBLIC_E2E_STATUS`;
- `G4_AUTOMATED_CHECKS_STATUS` (COMPLETE / PARTIAL / BLOCKED / NOT_STARTED);
- `L4`, `L5`, `GRAFANA`, `CLEANUP_STATUS`, `SLEEP_STATUS`.

`G3B_SECURITY_COMPLETE` requires every security phase to PASS **and** L4 PASS. `run` exits 0
only when security is complete, cleanup is COMPLETE and sleep is VERIFIED. A blocked gate
records what it observed (for example `L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE`, `GRAFANA_NOT_APPROVED`),
never PASS. These are technical checks; the official G4 screenshots are a separate owner session.

### Cleanup order (every path)

1. Stop new work.
2. Let an in-flight certbot finish (at most 10 min), then end it.
3. Disable `acmeHttp01`. By exact object identity, verify that the responder Deployment, Service, ConfigMap and HTTPRoute are gone, while every route the approved values enable (here feature-api, kagent-ui, agent-registry, grafana, plus https-redirect) and `edge-tls` are kept. The production certificate is never rolled back.
4. Dynu offline (signoff) when the recorded DNS state is `online` or `unknown`. No Dynu update is sent when DNS was never touched.
5. Whenever a public LB was created: a fresh, read-only `dns_check.py --expect-offline --offline-ip OFF4 --offline-ipv6 OFF6 --released-ip <LB>` (step `dns-verify-before-release`, 3 attempts). The recorded state (`untouched`, `unknown`, `online`, even `offline`) never replaces it. If the LB address is not in the state, it is read from the Service first.
6. Release the LB only on `DNS_OFFLINE_OK`. `NOT_YET` or `DNS_CHECK_FAILED` keeps the LB, records the verdict in `lb-release`, and gives `CLEANUP_INCOMPLETE`; guarded sleep still runs. After the owner fixes DNS, `cleanup` resumes (finished steps are skipped).
7. Fresh recovery manifest.
8. `plan-sleep`: only approved PDB blockers.
9. Guarded sleep with the scoped approval variables.
10. `verify --expect sleep` and an actual node count of 0.

A step that fails is recorded, the following safe steps still run, and `cleanup` can be run
again to resume.

### Owner G4 replay (separate session)

Claude does not take the official screenshots. After the G3-B campaign has ended and GKE is asleep, the owner:
1. Wakes the cluster under control.
2. Re-runs the repository scripts (table *G4 evidence replay* above: `public_e2e.py`, `negative_tests.py`).
3. Uses the real UIs and takes the screenshots.
4. Checks rows 37–43.
5. Runs cleanup and guarded sleep (`g3b_campaign.py cleanup`, or the Cloud Lab commands in step 16).

## Unattended (overnight) campaign (optional; superseded by the supervised run)

`scripts/edge/g3b_campaign.py` runs the procedure above as phases. State lives in
`$EV/state.json` (no secrets). There is a hard deadline of 3 h from the wake, and cleanup always
follows: on success, on failure, at the deadline, on SIGTERM/SIGINT, and through the watchdog
if the main process dies. `--simulate` records the commands instead of running them;
`tests/ops/test_dns_campaign.py` exercises every failure mode that way.

### What protects what

| Layer | Covers | Does not cover |
|---|---|---|
| Cleanup inside `run` (phase failure, deadline, SIGTERM/SIGINT) | test or deployment failures, the deadline, a terminal or Claude stop that sends a signal | `kill -9`, crash, power loss |
| Watchdog: systemd user timer every 5 min (`scripts/edge/systemd/`, `install_watchdog.sh`) | a dead main process (heartbeat older than 10 min and the pid gone), or a deadline the main process missed | laptop suspended, off or offline. `Persistent=true` runs a missed pass after resume, which is late, not prevented |
| Cloud-side fallback | the laptop itself failing | **not built** (needs owner approval) |

`systemd-inhibit` (in the service, and recommended for `run`) blocks suspend while cleanup or
the campaign runs; it cannot prevent power loss or a network drop.

### DNS before the LB (failure policy)

The LB is released only after a fresh `dns_check.py --expect-offline
--released-ip <LB IP>` (after any needed Dynu signoff, whatever state.json records) returns `DNS_OFFLINE_OK`. A `DNS_CHECK_FAILED` (timeout, SERVFAIL,
REFUSED, malformed reply, missing `aa`) never counts as offline. If DNS cannot be proven safe
within the bounded retries, the LB is **kept**: its address stays ours, so no record points
to someone else's address. The nodes still go to guarded sleep, `CLEANUP_STATUS` is
`CLEANUP_INCOMPLETE`, and `$EV/ALERT.txt` plus a desktop notification ask the owner to fix
DNS and run `g3b_campaign.py cleanup --state $EV/state.json` again (resumable). A kept LB with
no nodes costs a few cents an hour.

### Before overnight execution (all must hold; `init` checks them)

```bash
python3 scripts/edge/g3b_campaign.py init --campaign $CID --campaign-dir $EV --manifest $M0 \
  --approved-cidrs $PRIV/approved-cidrs.txt --edge-credentials $PRIV/edge-cred --grafana-credentials $PRIV/grafana-cred \
  --dns-credentials $PRIV/dynu.json --dns-offline-mode signoff --dns-offline-ip <OFF4> --dns-offline-ipv6 <OFF6> \
  --approve-guarded-sleep --approve-keda-pause --also-approve ewallet-mcp/feature-rag-mcp \
  --approve-production-certificate [--approve-grafana-root-url] --dns-offline-verified --dns-transition-evidence <stage 2 state.json> \
  (--accept-local-only-risk | --cloud-fallback-ready)
```

| Gate | Holds when |
|---|---|
| `CLOUD_LAB_RECOVERY_MANIFEST_VALID` | the manifest has its five files and is for `ewallet-dev` |
| `PDB_APPROVAL_COMPLETE` | `platform_checks.py sleep-feasibility` with the listed `--also-approve` covers every predicted blocker |
| `KEDA_SLEEP_APPROVAL_READY` / `GUARDED_SLEEP_PLAN_VALID` | the owner's approvals, and the approved record `sleep_20261106T163042Z` exists |
| `UNATTENDED_CLEANUP_READY` | DNS credentials present and the first transition proven (`--dns-transition-evidence`, *First DNS transition*); watchdog timer active; linger on; laptop risk covered (fallback ready, or risk explicitly accepted) |
| `CLIENT_ADDRESS_APPROVED` | this laptop's public address is inside the approved CIDRs (it is not recorded) |

Owner configuration:
1. `$PRIV/dynu.json` (mode 600): `{"hostname": "ewallet-hg.gleeze.com", "username": "<dynu user>", "password_sha256": "<sha256 of the IP update password>"}`. Create a **separate IP update password** in Dynu first.
2. `scripts/edge/install_watchdog.sh --enable`, `loginctl enable-linger $USER`, and point `~/ewallet-evidence/gateway/active-campaign` at `$EV`.
3. Keep the laptop on mains power and online. Start the run as `systemd-inhibit --what=sleep:idle python3 scripts/edge/g3b_campaign.py run --state $EV/state.json`.

### Dynu offline: dual-stack (owner observations, 2026-11-22)

| Method | Observed | Use |
|---|---|---|
| `dynu_dns.py offline --mode no-ipv4` (`myip=no&myipv6=no`) | Dynu answered `nochg`; the old IPv4 stayed in DNS (`DNS_OFFLINE_NOT_YET`) | **not** an offline method; kept only behind DNS verification |
| Dynu panel *Offline Settings → Go Offline Now* (action "Display offline message") | DNS changed on both families: A and AAAA point to Dynu's offline server; reverse DNS of both is `offline.dynu.com`, in the same ranges as `api.dynu.com` | the safe offline state |
| `dynu_dns.py offline --mode signoff` (`offline=yes`, IP Update Protocol) | 2026-11-22, sent while the hostname was already Offline: `good`, then `DNS_OFFLINE_OK`. The Online → Offline transition is **not yet proven** | the automation, once *First DNS transition* proves it |

A Dynu answer (`good`, `nochg`) only means the request was accepted. Only `dns_check.py`
decides. The offline addresses are campaign inputs, never defaults in the code. They are
re-read before each campaign, because Dynu may change them:

```bash
OFF4=$(dig +short @ns1.dynu.com $D A)        # while the hostname is Offline in the panel
OFF6=$(dig +short @ns1.dynu.com $D AAAA)
dig +short -x $OFF4; dig +short -x $OFF6     # both must be offline.dynu.com
python3 scripts/edge/dns_check.py --domain $D --expect-offline --offline-ip $OFF4 --offline-ipv6 $OFF6
```

Offline means each name, at every Dynu NS and at 1.1.1.1/8.8.8.8, answers A ∈ {none, OFF4} and
AAAA ∈ {none, OFF6}, never the released LB address. "None" counts only as a proven NXDOMAIN or
NODATA; a timeout is a failure. Online still means A = the LB address and **no AAAA**, not even
OFF6. The last JSON line summarises the verdict, failed servers and names, A/AAAA status and
duration (concurrent queries: about 3 s).

**API signoff verification.** This is now the `dns-transition` scope (*First DNS transition*,
stage 2): the online step publishes this campaign's own LB address, so no home or third-party
address is ever published. The earlier plan here (panel *Go Online* with the LB asleep) is
withdrawn because it could publish an address the owner does not control.

### During execution

Phases in order: `wake`, `baseline`, `temporary-certificate`, `edge-restricted`, `L0-live`,
`open-lb`, `dns-online`, `internet-gates`, `staging-certificate`, `production-certificate`,
`responder-off`, `grafana`, `public-e2e`, then cleanup.
- `python3 scripts/edge/g3b_campaign.py status --state $EV/state.json` shows `CURRENT_PHASE`, `phases`, `DEADLINE`, `LB_IP`, `DNS_TARGET_STATE` and the cleanup steps.
- The first failed phase stops new work.
- Wake, LB and DNS intents are written before each command, so a crash mid-command is still undone.

### After execution

| Check | Command | Expected |
|---|---|---|
| campaign | `g3b_campaign.py status --state $EV/state.json` | `CLEANUP_STATUS: COMPLETE`, `SLEEP_STATUS: VERIFIED`, `LAST_VERIFIED_NODE_COUNT: 0` |
| DNS | `dns_check.py --domain $D --expect-offline [--offline-ip …]` | `DNS_OFFLINE_OK` |
| no LB | `ownership_preflight.py live --context $CTX --kubeconfig $KCF` | `EDGE_OWNERSHIP_OK` |
| sleep | `cloud_lab.py status` and `verify --expect sleep --manifest <state.sleep_manifest>` | 0 VMs, `failures: []` |
| evidence | `$EV/*.jsonl`, `baseline.json`, `recovery-manifest/` | sanitized copies go to `docs/evidence/edai2/gateway/$CID/` |

### Recovery

| State | Action |
|---|---|
| `CLEANUP_INCOMPLETE`, step `dns-offline` | fix DNS (Dynu panel or credentials), then `g3b_campaign.py cleanup --state $EV/state.json`; it releases the kept LB once DNS is proven offline |
| step `sleep`: `SLEEP_BLOCKED` | a PDB blocker without approval: owner decides on `--also-approve`; never bypass |
| step `verify-sleep` | `cloud_lab.py status`; read `cleanup_steps` and the cloud_lab output; run cleanup again |
| watchdog `GAVE_UP` | three watchdog cleanups failed: owner action, see `ALERT.txt` |

### Cloud-side fallback (design only, not built)

Laptop failure is the gap. The smallest fallback that keeps the Cloud Lab guards:
- **What:** a Cloud Run job running a container with this repository, kubectl, helm and gcloud, which executes `g3b_campaign.py cleanup` against a state copy kept in a GCS object. A Cloud Scheduler trigger fires it once at the deadline plus 1 h, and the job does nothing if `CLEANUP_STATUS` is `COMPLETE`.
- **IAM:** a dedicated service account with `roles/container.admin` on the project (node-pool resize and guarded scaling), `roles/storage.objectAdmin` on one bucket, and Secret Manager accessor for the Dynu credentials.
- **Cost:** under $1 per month (Scheduler free tier, seconds of Cloud Run).
- **Risk:** a cloud identity able to resize the cluster, and a second copy of the DNS credentials.
- **Rollback:** delete the job, the trigger and the service account.

It is a separate, owner-approved change. Until then, unattended runs depend on the laptop staying on and online, with an estimated exposure of about $1.1/h if the laptop fails.

### PDB approval proposal for the next campaign

`ewallet-mcp/feature-rag-mcp` (PDB `minAvailable: 1`, Deployment of the `llm-agent-mcp` release, 2 replicas after `wake-full`):
- **Why it blocks:** its PDB blocks the drain to zero. The approved guarded record `sleep_20261106T163042Z` predates it, so it is not covered.
- **What the exception does:** before the resize, the guarded sleep scales this stateless MCP server from 2 to 0. It holds no data and no PVC; its state lives in the platform's stores. The fresh recovery manifest restores 2 at the next wake, and a failed sleep restores the replicas.
- **Why it must be re-approved:** earlier approvals were scoped to their campaign ID, so it is not carried over automatically.
- **How to verify:** `platform_checks.py sleep-feasibility` with and without the exception, then `cloud_lab.py plan-sleep` (`not_covered`) during cleanup.
