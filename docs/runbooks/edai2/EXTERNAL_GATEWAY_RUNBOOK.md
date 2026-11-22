# External gateway: public deployment (G3-B) and evidence replay (G4)

The replayable procedure for publishing the four edge routes on
`ewallet-hg.gleeze.com` and collecting the rubric evidence (rows 37–43). Every check is a
repository command run from the repository root. The architecture (Istio 1.30.7, Gateway API
v1.7.0, `ewallet-edge` + `ewallet-edge-gateway` in `ewallet-ingress`) is unchanged from G3-A1.
This runbook supersedes `G3B_DEPLOYMENT_PLAN.md` in the G3-B0 and G3-B1 evidence folders.

**Nothing here runs without the owner's approval of the campaign.** Steps marked
**[owner]** are done by the owner (Dynu, screenshots).

**Current way to run it:** *Canonical replay*, under *Supervised G3-B campaign*. That is the
orchestrator in supervised mode, reusing the trusted certificate already in `edge-tls`
(`--reuse-existing-production-certificate`), as verified live by Run 7
(`g3b-full-20261122T181805Z`, all technical checks PASS). The *Procedure* table below documents
what the orchestrator does step by step. Do not run it by hand: the orchestrator adds the
deadline, the cleanup ordering and the fail-closed gates.

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

Reference only: the steps `g3b_campaign.py` runs, written for **issuance mode** (the
historical path that created the certificate in run 6). In the current **reuse mode**:
- step 3 is replaced by the read-only `verify_edge_tls.py` check (*Reusing the issued production certificate*);
- step 4 is installed without the ACME responder;
- steps 7, 10 and the L4 test verify with system trust instead of `--ca-file <self-signed>`;
- steps 11–12 are skipped (`SKIPPED_REUSE`).

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
| 38 Logs UI | same run (row 38) | Loki returns log lines for `{k8s_namespace_name="ewallet-agents"}` | Grafana Explore → Loki query with recent lines |
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
| 1. Private deployment and checks | wake, baseline, gateway-ready, `tls-reuse-verify` (reuse mode) or temporary certificate (issuance mode), edge, L0, **edge-auth**, **private-e2e** | first phases of either scope | wake; edge upgrade; Secret sync only if mismatched and approved | the scope's approval, plus `--approve-edge-secret-sync` only when the Secret differs |
| 2. DNS transition verification (once) | LB opened on the checked edge, Offline → Online (LB) → signoff → Offline, then cleanup | `init --scope dns-transition` + `run` | LB (released at the end), two Dynu updates | `--approve-dns-transition-test` (not the G3-B live approval) |
| 3. Public deployment (G3-B) | LB, DNS online, Internet gates, L4, approved-network, certificate (reused and verified, or issued), Grafana, public E2E, evidence window | `init` (scope `g3b`) + `run`, see *Canonical replay* | everything in the procedure | `--approve-live-campaign` + the gated flags; `--dns-offline-verified` with stage 2's evidence |
| 4. Cleanup | ACME off, DNS offline proven, LB released, guarded sleep, verify | automatic; `g3b_campaign.py cleanup --state …` to resume | as listed | covered by the scope's approval |
| 5. Owner evidence | screenshots during a stage 3 campaign's evidence window | *Evidence window and screenshots* | none beyond stage 3 | the stage 3 approvals |

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

Observed, **VERIFIED** `g3b-dns-transition-ipv4-20261122T054437Z` (2026-11-22), after the owner set Dynu *Enable IPv6 Address* OFF and *Wildcard IPv6 Alias* OFF (TTL 120):
- Offline state now: A = OFF4, **AAAA none** (OFF6 stays an allowed offline value, but no longer appears).
- Private security: 8/8 and 35/35, edge auth MATCH, no mutation. The LB opened.
- Online (`myip=<LB>&myipv6=no`): `DNS_ONLINE_OK` at attempt 7 (about 3 min): A = LB at every Dynu NS and 1.1.1.1/8.8.8.8, **AAAA none on every name, including the wildcard**.
- Signoff: `DNS_OFFLINE_OK` at attempt 5 with `--released-ip <LB>`. The fresh check before release passed, the LB was released, sleep VERIFIED with 0 nodes, and no forwarding rule or address remained.
- This `state.json` passes `transition_evidence()`; it is the stage 3 `--dns-transition-evidence`.
- Keep Dynu IPv6 disabled. Re-enabling it brings back the AAAA = OFF6 online state (`g3b-dns-transition-retry-20261122T050543Z`).

`--dns-offline-verified` alone does nothing. Stage 3's `SAFE_CLEANUP_READY` needs
`--dns-transition-evidence` pointing at the `state.json` of a **real** transition run. That file must show:
- `scope: dns-transition`, `simulated: false` (`--simulate` runs are marked and always refused);
- `DNS_TRANSITION: VERIFIED` and the `dns-transition` phase `PASS`;
- `CLEANUP_STATUS: COMPLETE`, `SLEEP_STATUS: VERIFIED`, `LAST_VERIFIED_NODE_COUNT: 0`;
- cleanup steps `dns-verify-before-release` and `lb-release` ok (the LB was released after DNS was proven offline);
- the same domain, offline mode, OFF4 and OFF6.

A transition whose cleanup is incomplete (LB kept, sleep not verified) is not evidence; finish
it with `cleanup`, after which its `state.json` qualifies.

### Canonical replay (current, recommended)

```text
offline preflight → init → every pre-wake gate PASS → run → L4 (hotspot) → back to the approved
network → certificate verified (reused) → Grafana ready → final public E2E → evidence window →
owner screenshots → continue → automatic cleanup → verify 0 nodes
```

Run from the repository root, in one terminal for `run` and another for `status`:

```bash
D=ewallet-hg.gleeze.com; PRIV=~/ewallet-evidence/gateway/private
KC=~/.cache/ewallet-cloud-lab/kubeconfig; CTX=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
M0=~/ewallet-evidence/gateway/g3a1-20261121T175830Z/11-sleep/recovery-manifest
TRANSITION=~/ewallet-evidence/gateway/g3b-dns-transition-ipv4-20261122T054437Z/state.json
CID=g3b-full-$(date -u +%Y%m%dT%H%M%SZ); EV=~/ewallet-evidence/gateway/$CID

# 1. offline preflight (read-only)
python3 scripts/edge/dns_check.py --domain $D --expect-offline --offline-ip 162.216.242.207 --offline-ipv6 2602:ff23:0:8888::207 | tail -1   # DNS_OFFLINE_OK
python3 scripts/edge/verify_edge_tls.py --domain $D --kubeconfig $KC --context $CTX                        # TLS_REUSE_VERIFIED
bash scripts/edge/create_basic_auth.sh --credentials-file $PRIV/edge-cred --check --kubeconfig $KC --context $CTX   # EDGE_AUTH_MATCH
kubectl --kubeconfig $KC --context $CTX get nodes --no-headers | wc -l                                      # 0

# 2. init (read-only gates; nothing changes in the cloud)
python3 scripts/edge/g3b_campaign.py init --scope g3b --mode supervised --campaign $CID --campaign-dir $EV \
  --manifest $M0 --approved-cidrs $PRIV/approved-cidrs.txt --edge-credentials $PRIV/edge-cred \
  --grafana-credentials $PRIV/grafana-cred --dns-credentials $PRIV/dynu.json --dns-offline-mode signoff \
  --dns-offline-ip 162.216.242.207 --dns-offline-ipv6 2602:ff23:0:8888::207 \
  --dns-offline-verified --dns-transition-evidence $TRANSITION \
  --reuse-existing-production-certificate \
  --approve-live-campaign --approve-production-certificate --approve-grafana-root-url \
  --approve-guarded-sleep --approve-keda-pause --also-approve ewallet-mcp/feature-rag-mcp \
  --evidence-window-minutes 45
```

`init` exits 0 only when every gate is PASS. In reuse mode those are: `CLOUD_LAB_RECOVERY_MANIFEST_VALID`, `PDB_APPROVAL_COMPLETE`,
`KEDA_SLEEP_APPROVAL_READY`, `GUARDED_SLEEP_PLAN_VALID`, `CLIENT_ADDRESS_APPROVED`, `SAFE_CLEANUP_READY`,
`L0_RENDER_READY`, `EDGE_AUTH_READY`, `TLS_REUSE_READY`, `OWNER_LIVE_APPROVAL`. Any FAIL: stop, still no cloud
change. `--approve-edge-secret-sync` is **not** part of the canonical command. It is added only
with a separate approval, when `init` reports `edge_auth_at_init: EDGE_AUTH_MISMATCH`.

```bash
# 3. run (terminal 1) and watch (terminal 2)
systemd-inhibit --what=sleep:idle python3 scripts/edge/g3b_campaign.py run --state $EV/state.json
watch -n 30 "python3 scripts/edge/g3b_campaign.py status --state $EV/state.json | grep -E '\"(CURRENT_PHASE|TLS_CERTIFICATE_STATUS|L4|L5|CLIENT_NETWORK|GRAFANA|EVIDENCE_WINDOW|EVIDENCE_WINDOW_REMAINING_MIN|CLEANUP_STATUS|SLEEP_STATUS)\"'"

# 4. at CURRENT_PHASE: L4-denied, the owner moves the laptop to the hotspot; after L4: PASS, back to the
#    approved Wi-Fi, reconnecting once (see *Laptop network for the campaign*)

# 5. evidence window (EVIDENCE_WINDOW: open): owner screenshots, then
python3 scripts/edge/g3b_campaign.py continue --state $EV/state.json     # CONTINUE_ACCEPTED; cleanup follows

# 6. after cleanup
python3 scripts/edge/g3b_campaign.py status --state $EV/state.json | grep -E '"(CLEANUP_STATUS|SLEEP_STATUS|LAST_VERIFIED_NODE_COUNT|TLS_MODE|TLS_CERTIFICATE_STATUS)"'
kubectl --kubeconfig $KC --context $CTX get nodes --no-headers | wc -l                                      # 0
kubectl --kubeconfig $KC --context $CTX get svc -A | grep -c LoadBalancer                                   # 0
gcloud compute forwarding-rules list --project ewallet-data-ai-platform-hg26                              # empty
python3 scripts/edge/dns_check.py --domain $D --expect-offline --offline-ip 162.216.242.207 --offline-ipv6 2602:ff23:0:8888::207 | tail -1   # DNS_OFFLINE_OK
python3 scripts/edge/verify_edge_tls.py --domain $D --kubeconfig $KC --context $CTX                        # still TLS_REUSE_VERIFIED

# incomplete cleanup only: fix the reported cause (see *Evidence window and screenshots*, Incomplete cleanup), then resume
python3 scripts/edge/g3b_campaign.py cleanup --state $EV/state.json
```

Expected success (Run 7):
- `G3B_DEPLOYMENT_STATUS: PASS`, `G3B_SECURITY_STATUS: G3B_SECURITY_COMPLETE`, `G3B_PUBLIC_E2E_STATUS: PASS`, `G4_AUTOMATED_CHECKS_STATUS: COMPLETE`;
- `TLS_MODE: REUSE_EXISTING`, `TLS_CERTIFICATE_STATUS: VERIFIED_TRUSTED`, staging and production `SKIPPED_REUSE` (0 orders);
- `CLEANUP_STATUS: COMPLETE`, `SLEEP_STATUS: VERIFIED`, 0 nodes.

### Laptop network for the campaign

Campaign runs 3–5 lost Let's Encrypt and `kubectl` connections because the home Wi-Fi SSID is
served by two access points on different subnets, and the laptop roamed between them about 30 s
after each reconnect. For the campaign, the home Wi-Fi NetworkManager profile is temporarily
pinned to one BSSID, and both the home and hotspot profiles have `ipv6.method=disabled`. The
original values are backed up (no secrets) in `~/ewallet-evidence/gateway/private/networkmanager-*-backup.txt`.
- Before `init`:
  - `iw dev wlo1 link` shows the pinned BSSID;
  - `ip -6 addr show dev wlo1 scope global` prints nothing;
  - `curl -s -o /dev/null -w '%{http_code} %{remote_ip}\n' https://acme-v02.api.letsencrypt.org/directory` gives 200 and an IPv4 address.
- During the run: switch networks only when the campaign asks (L4), and reconnect **once**, using one method. After returning, don't change networks again until cleanup ends.
- After the last campaign, restore the recorded values:
  - `nmcli connection modify uuid <HOME-WIFI-UUID> 802-11-wireless.bssid ""`
  - `nmcli connection modify uuid <HOME-WIFI-UUID> ipv6.method auto`
  - `nmcli connection modify uuid <HOTSPOT-UUID> ipv6.method auto`
  - then reconnect once while at the laptop.

### Preconditions and owner approvals

The rest of this section is the full reference for every input and flag, including
**issuance mode**. Issuance mode is only needed if `TLS_REUSE_READY` fails (missing, expiring or
untrusted certificate); that is a separate owner decision, because it places a new Let's Encrypt order.

**Environment preflight for issuance mode only (before `init`; no cloud change).** `issue_certificate.sh` runs certbot
through `uvx`, which on the owner's laptop lives only in the `fintech` conda env. Append it to
`PATH` (appending keeps the system `python3` first) in the terminal that runs `init` and `run`:

```bash
export PATH="$PATH:$HOME/miniconda3/envs/fintech/bin"
command -v python3                                   # /usr/bin/python3
uvx --from certbot==5.10.0 certbot --version         # certbot 5.10.0
cat /sys/class/power_supply/AC*/online               # 1 (on AC power)
D=ewallet-hg.gleeze.com; PRIV=~/ewallet-evidence/gateway/private
M0=~/ewallet-evidence/gateway/g3a1-20261121T175830Z/11-sleep/recovery-manifest
TRANSITION=~/ewallet-evidence/gateway/g3b-dns-transition-ipv4-20261122T054437Z/state.json
CID=g3b-full-$(date -u +%Y%m%dT%H%M%SZ); EV=~/ewallet-evidence/gateway/$CID
python3 scripts/edge/dns_check.py --domain $D --expect-offline --offline-ip 162.216.242.207 --offline-ipv6 2602:ff23:0:8888::207   # DNS_OFFLINE_OK
```

In issuance mode `init` checks it again (gate `CERTBOT_READY`; in reuse mode the gate is `TLS_REUSE_READY` and certbot is not needed). Dynu IPv6 must stay disabled (*First DNS transition*).

**Rotating the edge credential (owner, local only).** Do this when the edge password may have been
exposed (G3-B 2026-11-22: a test recorded it inside a redirect URL; fixed, see below). The new
password is generated straight into a mode-600 file and never shown, typed, pasted or logged:

```bash
umask 077
python3 - <<'PY'
import os, secrets
path = os.path.expanduser("~/ewallet-evidence/gateway/private/edge-cred.new")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # refuses to overwrite
with os.fdopen(fd, "w") as handle:
    handle.write("edgeadmin:" + secrets.token_urlsafe(36) + "\n")   # 48 random characters, never reused
print("written:", path)
PY
stat -c '%a %s' ~/ewallet-evidence/gateway/private/edge-cred.new      # 600, about 59 bytes; never cat it
mv ~/ewallet-evidence/gateway/private/edge-cred ~/ewallet-evidence/gateway/private/edge-cred.retired
mv ~/ewallet-evidence/gateway/private/edge-cred.new ~/ewallet-evidence/gateway/private/edge-cred
```

The Kubernetes Secret is not touched here. The next campaign's `init` reports
`edge_auth_at_init: EDGE_AUTH_MISMATCH`, so `EDGE_AUTH_READY` fails unless the owner approves
`--approve-edge-secret-sync` for that campaign. With the approval, phase `edge-auth` runs, in order:
1. `create_basic_auth.sh --check` detects the mismatch;
2. `--apply` (the password is read from the file, never an argument);
3. gateway `rollout restart`;
4. `rollout status --timeout=300s`;
5. `platform_checks gateway`;
6. `--check` again (MATCH);
7. `private-e2e` proves the new credential before any LB.

A failure at any step stops the campaign before the LB, and cleanup runs. After a successful
campaign, delete `edge-cred.retired` (`shred -u`) and clear the old password from the browser.

**Recorded results never contain credentials.** `negative_tests.py` and `public_e2e.py` strip
URL userinfo from every redirect they record (curl's `%{redirect_url}` carries the request
credentials). Before anything is printed or written, they also replace every form of the edge
and Grafana credentials (raw, URL-encoded, Basic base64) with `<redacted>`.

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
| Screenshot window after a passed public-e2e (supervised g3b only, 1–60 min) | `--evidence-window-minutes 45` |

Issuance-mode `init` (historical; the canonical command is in *Canonical replay*):

```bash
python3 scripts/edge/g3b_campaign.py init --mode supervised --campaign $CID --campaign-dir $EV --manifest $M0 \
  --approved-cidrs $PRIV/approved-cidrs.txt --edge-credentials $PRIV/edge-cred --grafana-credentials $PRIV/grafana-cred \
  --dns-credentials $PRIV/dynu.json --dns-offline-mode signoff --dns-offline-ip <OFF4> --dns-offline-ipv6 <OFF6> \
  --dns-offline-verified --dns-transition-evidence $TRANSITION --approve-live-campaign --approve-guarded-sleep --approve-keda-pause --also-approve ewallet-mcp/feature-rag-mcp \
  --approve-production-certificate --approve-grafana-root-url --evidence-window-minutes 45
```

(`<OFF4>`/`<OFF6>`: `162.216.242.207` / `2602:ff23:0:8888::207`, re-read before the campaign.)

`init` must print every gate as PASS:
- `CLOUD_LAB_RECOVERY_MANIFEST_VALID`, `PDB_APPROVAL_COMPLETE`, `KEDA_SLEEP_APPROVAL_READY`, `GUARDED_SLEEP_PLAN_VALID`;
- `CLIENT_ADDRESS_APPROVED`;
- `SAFE_CLEANUP_READY` (DNS automation);
- `L0_RENDER_READY` (the exact public render passes the strict preflight, offline);
- `EDGE_AUTH_READY` (Secret = `edge-cred`, or a mismatch with `--approve-edge-secret-sync`);
- `CERTBOT_READY` in issuance mode (`uvx --from certbot==5.10.0 certbot --version` answers `certbot 5.10.0`), or `TLS_REUSE_READY` in reuse mode (`verify_edge_tls.py` verifies `edge-tls`, plus `--approve-production-certificate`);
- `OWNER_LIVE_APPROVAL`.

Supervised mode does not require the watchdog, linger or laptop-risk acceptance. Everything
that protects security and data is still required.

### Run, watch, stop

```bash
systemd-inhibit --what=sleep:idle python3 scripts/edge/g3b_campaign.py run --state $EV/state.json
python3 scripts/edge/g3b_campaign.py status --state $EV/state.json      # in another terminal
watch -n 30 "python3 scripts/edge/g3b_campaign.py status --state $EV/state.json | grep -E '\"(CURRENT_PHASE|L4|L5|CLIENT_NETWORK|GRAFANA|EVIDENCE_WINDOW|EVIDENCE_WINDOW_REMAINING_MIN|CLEANUP_STATUS|SLEEP_STATUS)\"'"
```

| Behaviour | What happens |
|---|---|
| Deadline | 3 h from the wake; no step starts after it. Expected use: about 1 h to the end of public-e2e (live DNS-transition runs reached the open LB 12 min after wake; L4 can take up to 20 min), then the 45-min window, then cleanup (about 18 min, never cut short by the deadline) |
| Commands | each runs in its own process group with a timeout bounded by the deadline (max 30 min); a timeout ends the whole group |
| Stop safely | Ctrl-C or `kill -TERM <main pid>` (shown in `status`): the running command is ended, then cleanup runs. A second signal does not interrupt cleanup |
| Main process hung | `kill -9 <main pid>`, then `g3b_campaign.py cleanup --state $EV/state.json` (resumable). The optional watchdog does the same after a stale heartbeat, and sends SIGTERM to a main process still running 15 min past the deadline. It never cleans up while the main process holds the campaign lock |

### Gates and where they run

| Gate | Phase | Evidence class | Notes |
|---|---|---|---|
| L0 render | `init` and `L0-render` | LOCAL (render) | exact public values; `ownership_preflight.py render --allow-public-loadbalancer` |
| L0 live | `L0-live` | GKE_PRIVATE | active Envoy filters, RBAC CIDRs = approved; **if L0 fails, no LB** |
| Gateway Deployment Ready | `gateway-ready` (after wake and baseline, before any edge change) | GKE_PRIVATE | read-only: `ewallet-edge-gateway` must want ≥ 1 replica, finish `kubectl rollout status` and report a Ready replica within one 25-minute budget, capped by the deadline. Otherwise FAIL, no edge change, no LB, normal cleanup and sleep. See *Gateway readiness after wake* |
| Edge Basic Auth | `edge-auth` | GKE_PRIVATE | `create_basic_auth.sh --check`; on an approved mismatch `--apply`, gateway restart, rollout status, `platform_checks gateway`, `--check`. Nothing secret is printed or stored. **A mismatch without approval, or a sync that does not take, means no LB** |
| Private E2E | `private-e2e` | GKE_PRIVATE | port-forward; Feature API, Registry, Grafana (if credentials), filters; agents must be **403** (the port-forward peer is never approved); `negative_tests.py`: missing/wrong/forged credentials 401, valid accepted, rate limit 429. A gateway still holding the old users file fails here. It is not L4. **If it fails, no LB** |
| L2/L3/L6 | `internet-gates` | PUBLIC_INTERNET | `negative_tests.py --rate-limit-mode http2` |
| L5 | `internet-gates` | PUBLIC_INTERNET | this machine must be inside the approved CIDRs (checked first) |
| L4 | `L4-denied` | PUBLIC_INTERNET, **only from a non-approved address** | this laptop on another network; otherwise `L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE` |
| Back on an approved network | `approved-network` | client address (IPv4, not recorded) | waits up to 15 min for this laptop to be inside the approved CIDRs again; outside or unverifiable means FAIL and cleanup (public-e2e needs an approved client) |
| L1 | `production-certificate` + `public-e2e` (row 43) | PUBLIC_INTERNET | system trust, 4 SANs, TLS 1.1 refused |
| L7/L8 | `public-e2e` | GKE_PRIVATE / PUBLIC_INTERNET | ownership preflight live; regression against the baseline |
| L9 / rows 37–39 | `grafana` + `public-e2e` | PUBLIC_INTERNET | `release_diff.py` must be clean before the one-line upgrade; after the upgrade the new Grafana pod must be Ready (*Grafana rollout readiness*); rows 37–39 must pass, else `GRAFANA_VERIFICATION_*` |
| Owner evidence window | `evidence-window` | none (a pause) | only after a passed public-e2e; see *Evidence window and screenshots* |

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

**Hotspot steps (owner).** kubectl keeps working on any network (the kubeconfig uses the GKE DNS
endpoint, IAM-authenticated), so the orchestrator keeps running while the laptop moves.
1. Stay on the approved network until `CURRENT_PHASE: L4-denied` (L5 has passed by then).
2. Move this laptop to the phone hotspot. Do not stop the campaign.
3. Wait for `L4: PASS (this client, outside the approved CIDRs before and after)`, about one minute.
4. Move back to the approved network at once. `approved-network` waits at most 15 minutes and shows `CLIENT_NETWORK: approved`.
5. If the hotspot address happens to be inside the allowlist, L4 ends `L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE`. The run continues safely, but security is INCOMPLETE.

Client addresses are looked up with `dig -4` (the gateway sees IPv4) and never written to the
state or evidence.

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

### Gateway readiness after wake

What happened in the first Full G3-B attempt, `g3b-full-20261122T070154Z` (2026-11-22):
- `edge-restricted` failed about 7 minutes after the wake. `ewallet-edge-gateway` had 0 pods: during the wake, its ReplicaSet's pod creation was rejected (`failed calling webhook "object.sidecar-injector.istio.io"`) because istiod was still starting.
- Kubernetes retries a failed pod creation with exponential back-off, up to about 17 minutes, so the pod appears late.
- The run stopped fail-closed: no LB, DNS untouched, cleanup COMPLETE, 0 nodes.

Phase `gateway-ready` now waits for the Deployment itself; `platform_checks.py gateway` only checks Gateway API conditions. The gate:
- requires at least one desired replica, so a Deployment scaled to 0 never passes;
- runs `kubectl rollout status deployment/ewallet-edge-gateway --timeout=<remaining>s`, asked again if it returns early;
- requires at least one Ready replica;
- uses one 25-minute budget, capped by the campaign deadline.

It restarts, deletes or bypasses nothing: no istiod restart, no injection-policy change. If the Deployment is not Ready in time, the phase fails before any edge change, and cleanup and guarded sleep run as usual.

The next wake keeps the G3-A1 recovery manifest. The manifest recorded by that failed run shows the gateway with 0 ready pods and must not replace it.

### Let's Encrypt production: one bounded retry

What happened in the third Full G3-B attempt, `g3b-full-20261122T083830Z` (2026-11-22):
- Every phase up to and including Let's Encrypt staging passed.
- The production step then failed. certbot read the directory, then timed out (45 s, `requests.exceptions.ReadTimeout`) on `POST /acme/new-acct`, the account registration.
- No `new-order` request had been sent, and no certificate was received.
- Cleanup was COMPLETE: signoff, fresh DNS check, LB released, 0 nodes.

The production phase now tries **once more, after 60 s**, only when that attempt's own certbot log (`private/tls/logs/letsencrypt.log`) proves all of the following:
- it was written by this attempt;
- the last request was `new-acct`;
- no order, finalize or challenge request was sent;
- no certificate was received, and none exists in `config/live/edge`;
- the final error is a network timeout.

In addition, no signal may have arrived, the first certbot process group must be gone, and at least 20 minutes must remain before the deadline.

Anything else fails closed with no retry: HTTP-01 failure, rate limit, `unauthorized`, ambiguous or stale logs, a received certificate that failed to import, a staging failure. There are at most two production attempts, run one after the other with the same certbot configuration and directories.

`state.json` records `ACME_PRODUCTION_ATTEMPTS`, `ACME_PRODUCTION_RETRY_REASON` (or `no retry: …`) and `ACME_PRODUCTION_FINAL_STATUS`, and no ACME material. A second failure fails the phase, and cleanup runs as usual.

### Reusing the issued production certificate (`--reuse-existing-production-certificate`)

The sixth Full G3-B run issued a trusted Let's Encrypt certificate (issuer YE1, the 4 names, valid
until 2027-02-20) into `ewallet-ingress/edge-tls`. That Secret survives guarded sleep and wake.
Without the flag, every campaign still does what it always did: it imports a self-signed
certificate into `edge-tls`, then orders a new staging and production certificate. That means one
production order per run, against Let's Encrypt's limit of 5 duplicate certificates per week.

With the flag (`init`, scope `g3b`), the campaign keeps the existing certificate. Read-only, first
in `init` (gate `TLS_REUSE_READY`, which also requires `--approve-production-certificate`) and again
in phase `tls-reuse-verify` (in place of `temporary-certificate`), `scripts/edge/verify_edge_tls.py`
checks that:
- the chain is trusted by the system CA bundle;
- all 4 names are present;
- at least 14 days are left;
- the private key matches the certificate (compared by public key; the key is read into memory only, never printed or written).

Then:
- **No self-signed import and no ACME responder.** `ewallet-edge` is installed with `acmeHttp01` off, and the L0 render matches.
- **No order.** `staging-certificate` and `production-certificate` record `SKIPPED_REUSE`; `ACME_PRODUCTION_ATTEMPTS: 0`; never `ISSUED`.
- **System trust everywhere.** The private (port-forward, with the real hostnames), Internet and L4 checks verify the real certificate against system trust instead of `--ca-file <self-signed>`, and the final public E2E (row 43) still requires a trusted handshake.
- **Verdicts say what happened.** `TLS_MODE: REUSE_EXISTING`, `TLS_CERTIFICATE_STATUS: VERIFIED_TRUSTED`, `TLS_CERTIFICATE_SOURCE: EXISTING_EDGE_TLS`. A verified reuse satisfies the TLS part of the deployment and security verdicts.
- **Fail closed.** A certificate that is missing, expiring, untrusted, missing a name, mismatched with its key or unreadable stops the campaign. There is no self-signed fallback and no automatic issuance; issuing a new one is a separate owner decision. Cleanup never touches `edge-tls`.

### Grafana rollout readiness

What happened in the sixth Full G3-B run, `g3b-full-20261122T164612Z` (2026-11-23):
- 18 of 19 phases passed: the Let's Encrypt production certificate was issued and trusted, and the root URL was applied (`llm-agent` revision 61).
- `public-e2e` then got **HTTP 503** for rows 37–39. The successful `helm upgrade` had changed Grafana's environment, which replaced its pod. `public-e2e` started while the old pod was stopping and the new one was not yet Ready, so the gateway had no healthy Grafana backend.
- About 55 s later the new pod was Ready, with `GF_SERVER_ROOT_URL` set.

A successful `helm upgrade` only means the new Deployment spec was accepted, not that the new pod serves. So the `grafana` phase now continues only after a read-only check:
1. `kubectl -n <namespace.name of the release> rollout status deployment/grafana --timeout=<budget>s`;
2. at least one ready replica (`.status.readyReplicas`).

The budget is at most 300 s and never more than the time left before the campaign deadline. `ROOT_URL_APPLIED` is recorded only after the check passes.

If the rollout does not complete or no replica is Ready, the phase **fails** (`GRAFANA_VERIFICATION_FAILED: Grafana not Ready …`). `public-e2e` does not run, and cleanup and guarded sleep run as usual. Nothing is restarted, scaled or deleted.

A failed `helm upgrade` is unchanged: no wait, `GRAFANA_VERIFICATION_FAILED: upgrade exit N`. The two `release_diff` checks and the upgrade command are unchanged.

### Evidence window and screenshots (rows 37–43)

With `--evidence-window-minutes 45`, the campaign stops after a **passed** `public-e2e` and holds
the public edge unchanged: trusted certificate, Grafana root URL, Basic Auth, source restriction
and rate limit all in place. `status` shows `EVIDENCE_WINDOW: open` and
`EVIDENCE_WINDOW_REMAINING_MIN`.

- The window closes on `python3 scripts/edge/g3b_campaign.py continue --state $EV/state.json` (accepted only while it is open), when the minutes run out, or when only 30 minutes remain before the deadline. Cleanup then starts by itself.
- Ctrl-C also ends it, with cleanup; the run is then recorded as stopped.
- A failed `public-e2e` never opens the window. The window changes no verdict and runs no command.

Screenshots are taken by the owner, from the real deployment, to illustrate the technical
documentation (gateway, DNS/TLS, security policy, routing, observability, lifecycle). They are
saved as `~/Pictures/Screenshots/GW-<subject>.png`. When to take them:

| Moment | What exists then | What to capture |
|---|---|---|
| `CURRENT_PHASE: L4-denied`, laptop on the hotspot, after `L4: PASS` | the public edge, seen from a non-approved address | GW-L4-403: `https://agents.$D/` in a private browser window, **403** from the source restriction. The certificate is trusted in reuse mode. Then return to the approved Wi-Fi at once: `approved-network` waits at most 15 min |
| `EVIDENCE_WINDOW: open` | public LB, DNS online, trusted certificate, Grafana root URL | GW-routes and GW-dns: `kubectl --kubeconfig $KC --context $CTX get gateway,httproute -n ewallet-ingress` and `dig +short features.$D` (the LB address); then the UI and HTTPS shots below |
| after the window closes and cleanup ends | DNS offline, no LB, 0 nodes | `status` verdicts and cleanup steps, the node count, `dns_check.py --expect-offline` |

During the evidence window:

| ID | Row | Screenshot | Required |
|---|---|---|---|
| GW-43-padlock | 43 | padlock and certificate details on `https://features.$D/` (Let's Encrypt, 4 names) | yes |
| GW-43-api | 43 | terminal: authenticated Feature API lookup over HTTPS (422/424 = application reached) | yes |
| GW-42-dialog | 42 | the browser's Basic Auth dialog | yes |
| GW-42-401 | 42 | terminal: `curl -sI https://features.$D/` → 401 with `www-authenticate: Basic realm="e-wallet"` | yes |
| GW-37-metrics | 37 | `https://observe.$D/` dashboard with live panels (edge auth, then Grafana login) | yes |
| GW-38-logs | 38 | Grafana Explore → Loki `{k8s_namespace_name="ewallet-agents"}` with recent lines | yes |
| GW-39-traces | 39 | Grafana Explore → Tempo, one trace with spans | yes |
| GW-40-agents | 40 | `https://agents.$D/`, one short synthetic prompt answered | yes |
| GW-41-registry | 41 | `https://registry.$D/` listing an agent | yes |

These can also be taken after cleanup, from the evidence files or with 0 nodes:
- GW-42-429: the `14-negative.jsonl` burst line (required);
- GW-L4-403 fallback, if the hotspot shot was missed: `11-l4-denied.jsonl` (optional);
- GW-verdict: `g3b_campaign.py status` (optional);
- GW-sleep: `cloud_lab.py verify --expect sleep` plus `dns_check.py --expect-offline` (optional).

Record `sha256sum ~/Pictures/Screenshots/GW-*.png` in the campaign evidence.

**Automated PASS is not screenshot evidence.** `G4_AUTOMATED_CHECKS_STATUS: COMPLETE` means the
repository checks passed. Rows 37–43 are `EVIDENCE_COMPLETE` only when the owner's required
screenshots exist from the same campaign.

**Grafana root URL after cleanup.** The approved upgrade sets `GF_SERVER_ROOT_URL` to
`https://observe.$D/`, and cleanup does not revert it. After the campaign, Grafana opened through
a local port-forward may redirect to that (offline) public URL. A revert is a separate,
owner-approved `helm upgrade llm-agent` without the `--set`.

**Incomplete cleanup.** `status` lists `cleanup_failed_steps`, and `$EV/ALERT.txt` explains them.
A kept LB means DNS could not be proven offline: the owner uses the Dynu panel *Go Offline Now*,
runs `dns_check.py --expect-offline …` until `DNS_OFFLINE_OK`, then runs
`g3b_campaign.py cleanup --state $EV/state.json`. It resumes and skips finished steps. Never two
cleanups at once (the campaign lock refuses a second).

### Owner G4 replay (separate session)

Only if a screenshot was missed. Claude does not take the official screenshots. Do not wake the
cluster by hand: the public edge needs the gateway, DNS and cleanup prerequisites that only the
orchestrator enforces. Instead, run a new, separately approved campaign with the *Canonical replay*
command (reuse mode: no new certificate order), and take the missing screenshots in its evidence
window. The table *G4 evidence replay* above lists the checks that campaign runs (`public_e2e.py`,
`negative_tests.py`).

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
| Dynu panel *Offline Settings → Go Offline Now* (action "Display offline message") | (with IPv6 enabled) DNS changed on both families: A and AAAA point to Dynu's offline server; reverse DNS of both is `offline.dynu.com`, in the same ranges as `api.dynu.com` | the safe offline state |
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
