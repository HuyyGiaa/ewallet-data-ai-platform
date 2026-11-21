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
| P2 | DNS before | `python3 scripts/edge/dns_check.py --domain $D --expect-offline` | records the starting shape (values withheld); exit 2 = `DNS_CHECK_FAILED` (state not provable) |
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

## Unattended (overnight) campaign

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

The LB is released only after `dynu_dns.py offline` and `dns_check.py --expect-offline
--released-ip <LB IP>` return `DNS_OFFLINE_OK`. A `DNS_CHECK_FAILED` (timeout, SERVFAIL,
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
  --dns-credentials $PRIV/dynu.json --dns-offline-mode <no-ipv4|signoff> [--dns-offline-ip <placeholder>] \
  --approve-guarded-sleep --approve-keda-pause --also-approve ewallet-mcp/feature-rag-mcp \
  --approve-production-certificate [--approve-grafana-root-url] --dns-offline-verified \
  (--accept-local-only-risk | --cloud-fallback-ready)
```

| Gate | Holds when |
|---|---|
| `CLOUD_LAB_RECOVERY_MANIFEST_VALID` | the manifest has its five files and is for `ewallet-dev` |
| `PDB_APPROVAL_COMPLETE` | `platform_checks.py sleep-feasibility` with the listed `--also-approve` covers every predicted blocker |
| `KEDA_SLEEP_APPROVAL_READY` / `GUARDED_SLEEP_PLAN_VALID` | the owner's approvals, and the approved record `sleep_20261106T163042Z` exists |
| `UNATTENDED_CLEANUP_READY` | DNS credentials present and the offline mode verified once (below); watchdog timer active; linger on; laptop risk covered (fallback ready, or risk explicitly accepted) |
| `CLIENT_ADDRESS_APPROVED` | this laptop's public address is inside the approved CIDRs (it is not recorded) |

Owner configuration:
1. `$PRIV/dynu.json` (mode 600): `{"hostname": "ewallet-hg.gleeze.com", "username": "<dynu user>", "password_sha256": "<sha256 of the IP update password>"}`. Create a **separate IP update password** in Dynu first.
2. `scripts/edge/install_watchdog.sh --enable`, `loginctl enable-linger $USER`, and point `~/ewallet-evidence/gateway/active-campaign` at `$EV`.
3. Keep the laptop on mains power and online. Start the run as `systemd-inhibit --what=sleep:idle python3 scripts/edge/g3b_campaign.py run --state $EV/state.json`.

**One-time Dynu offline verification** (owner-approved DNS mutation; the cluster can stay
asleep). Dynu documents neither offline mode's DNS answer, so prove it:
1. `python3 scripts/edge/dynu_dns.py --credentials $PRIV/dynu.json --execute offline --mode no-ipv4`. Expected: `DNS_OFFLINE_OK` (NXDOMAIN or NODATA everywhere). This also stops the names pointing at the home address.
2. If it is not `DNS_OFFLINE_OK`: `… offline --mode signoff`, then read which address is answered. If it is a Dynu address the owner accepts, record it as `--dns-offline-ip`.
3. Use the mode that verified as `--dns-offline-mode`, and pass `--dns-offline-verified` to `init`.

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
