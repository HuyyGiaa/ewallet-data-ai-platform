# Vault + ESO GKE acceptance campaign (prepared, not executed)

Gate-by-gate plan for deploying the existing Vault + External Secrets Operator implementation
([VAULT.md](../../edai2/platform/VAULT.md)) on `ewallet-dev`, and for capturing the owner's evidence.
Commands come from [VAULT_ESO_RUNBOOK.md](VAULT_ESO_RUNBOOK.md) (cited as *RB step N*) and
`ops/gcp/cloud_lab.py`.

**Status: Stage A (preparation) complete; Stage B (live) not started.** Nothing here runs without
the owner's explicit approval of the campaign. Every **[OWNER]** step is performed by the owner
personally. Nothing in this page wakes GKE.

## 1. Baseline (2026-11-24)

| Item | Value |
|---|---|
| Source | `feature/edai2-vault-eso` (based on `feature/edai2-external-gateway-api` 4ba50241; never on main) |
| Cluster | `ewallet-dev`, `asia-southeast1-b`, GKE 1.36.4-gke.2377000, REGULAR channel, Calico NetworkPolicy enforced, not a private-node cluster |
| Node pool | `cpu-pool`, e2-standard-4, single zone b, no autoscaling, **0 nodes (asleep)** |
| Access | `KUBECONFIG=~/.cache/ewallet-cloud-lab/kubeconfig` (DNS endpoint); context `gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev` |
| Wake manifest | `~/ewallet-evidence/gateway/g3b-full-20261122T181805Z/recovery-manifest`: the newest under `~/ewallet-evidence`, written by `cloud_lab record-manifest` at 2026-11-22T19:03:36Z, before the last guarded sleep (73 workloads, ≈10.3 vCPU and 24.8 GiB of requests, 5 nodes) |
| Read-only preflight | `--mode preinstall`, [`gke_preflight_20261124T060932Z`](../../evidence/edai2/vault/gke_preflight_20261124T060932Z/gates-preinstall.jsonl): `VAULT_GKE_PREFLIGHT_READY`; `cert-manager-ready` NOT_CHECKED (asleep) |

### Preflight modes

`scripts/vault/gke_preflight.py` is read-only (get, list, version, template). It fails closed:
a failed or unreadable query is FAIL (`QUERY_FAILED` or `DATA_INVALID`), never PASS and never "absent".

| Mode | When | Checks |
|---|---|---|
| `preinstall` | G1 (and now, asleep) | no earlier Vault/ESO namespace, CRD, release or PVC; cert-manager CRDs and, when awake, its deployments; `standard-rwo`; `feast-online-redis` present and non-empty; `ewallet-serving` release, drift and overlay scope |
| `consumer-switch` | G8, before the upgrade | cluster awake; `vault` release deployed; ESO's 3 deployments available; ExternalSecret `SecretSynced`; target Secret owned by the ExternalSecret and non-empty; `SHADOW_MATCH`; Feature API available and still on `feast-online-redis`; release drift and overlay scope. Existing Vault/ESO resources are expected here and never block |

### Preflight gates (now)

| Gate | Expected | Observed | Status | Required action |
|---|---|---|---|---|
| Git baseline | Vault branch on the gateway branch; no merges from main; clean tree | as expected | PASS | — |
| Cluster reachable (live) | API answers on the pinned context | v1.36.4-gke.2377000 | PASS | — |
| No earlier Vault/ESO (live) | no namespace, ESO CRD, release or Vault PVC | confirmed absent | PASS | none; nothing to adopt or re-initialise |
| cert-manager installed (live) | Certificate and Issuer CRDs | v1.16.2 release, CRDs present | PASS | — |
| cert-manager running | 3 deployments available | 0 nodes | NOT_CHECKED | G1 preflight |
| cert-manager → Vault TLS → ESO chain | `certificate.yaml` issues certificates that name `vault.ewallet-vault.svc.cluster.local`; ESO verifies them | [`eso_local_20261124T055021Z`](../../evidence/edai2/vault/eso_local_20261124T055021Z/results.jsonl): 65/65 on kind with cert-manager v1.16.2 | PASS (kind) | G2 repeats it on GKE |
| StorageClass (live) | `standard-rwo`, PD CSI, WaitForFirstConsumer | as expected; reclaim **Delete** | PASS | G2: owner-approved `Retain` before G3 |
| Disk zone vs pool | zonal disks schedulable after a wake | pool and disks in zone b only | PASS | — |
| Vault drain behaviour | no PDB that blocks a resize to zero | chart default `maxUnavailable: 0` disabled in `values-gke.yaml`; verified on kind | PASS (source) | G2 confirms on GKE |
| Redis Secret (live) | `feast-online-redis` with non-empty `connection_string` | present; created out of band 2026-10-31; also read by Fraud API; copies exist in `ewallet-agents`, `ewallet-mcp` | PASS | copy only, never rotate |
| Serving release (live) | one release owns Feature API | `ewallet-serving` rev 3 (`ml-platform`): Feature API Deployment/SA/Services, `fraud-api-metrics`, ScaledObjects `feature-api` and `fraud-api`, 3 NetworkPolicies | PASS | — |
| Release drift / overlay scope (live) | identical; one-line change | identical; only `feast-online-redis` → `feature-api-redis-vault` | PASS | G8 re-checks in `consumer-switch` mode |
| Business lookup baseline | the fixture returns features | not callable while asleep (§4) | NOT_CHECKED | G1 decides the G9 verdict class |

## 2. Risks

| Class | Risk | Handling |
|---|---|---|
| **Owner-accepted lab risk** (2026-11-24) | The CI identity `ewallet-ci@…` holds `roles/container.developer`, which includes `container.serviceAccounts.createToken`. CI can therefore request ServiceAccount tokens for the Vault identities: `vault-admin`, to start or cancel a root-generation ceremony (never finish it without 2 shares), and `eso-vault-reader`, to read what that reader may read. Cluster-admin, and after G6 the ESO controller, can do the same. **This is not an isolation boundary**; it is accepted for the lab. | Documented; no IAM change in this campaign. Removing it later is a separate owner decision |
| IMPORTANT | `standard-rwo` deletes a disk with its PVC | G2 `Retain` before G3 (RB step 2); never delete the namespace or PVCs |
| IMPORTANT | The online store may not hold the fixture entity (§4) | G1 baseline decides the G9 verdict class; no materialization in this campaign |
| IMPORTANT | GKE may upgrade nodes mid-campaign (REGULAR channel, no maintenance window) | Vault restarts sealed: unseal again. Consumers keep their Secrets |
| OPTIONAL | Vault keeps its certificate until restart; cert-manager renews at day 75 | Each wake restarts Vault; RB step 12 has the check |
| OPTIONAL | `cloud_lab.py evaluate_pdbs` ignores `maxUnavailable: 0` budgets | Not triggered (none exist; Vault's is disabled). A tool fix is a separate change |

## 3. Campaign gates (Stage B)

Campaign ID `vault-gke-<UTC timestamp>` (`$CID`). Working directory `~/ewallet-evidence/vault/$CID/`
(mode 700); recovery material only in `~/ewallet-evidence/vault/private/`. On every command:
`export KUBECONFIG=~/.cache/ewallet-cloud-lab/kubeconfig`, plus `--context $CTX` (`--kube-context` for helm).
`P="python3 scripts/vault/gke_preflight.py --kubeconfig $KUBECONFIG --context $CTX --out ~/ewallet-evidence/vault/$CID/preflight"`.

| Gate | Owner approval | Actions (RB = runbook step) | Pass when | Stop / rollback |
|---|---|---|---|---|
| **G1 Wake and baseline** | **[OWNER] approve the wake** | `CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest ~/ewallet-evidence/gateway/g3b-full-20261122T181805Z/recovery-manifest --nodes 5 --confirm-cluster ewallet-dev --execute`; `cloud_lab.py verify --expect awake --manifest <same>`; `$P --mode preinstall`; record the `feast-online-redis` resourceVersion, `helm -n ml-platform history ewallet-serving`, the Feature API `secretKeyRef`, and the Gateway release revisions (`ewallet-edge` 21, `ewallet-edge-gateway` 16); baseline lookup (§4) | preinstall READY with `cert-manager-ready` PASS; workloads healthy; baseline lookup recorded | Unhealthy platform or preflight BLOCKED/FAIL: stop, then sleep (§6) |
| **G2 Vault infrastructure** | **[OWNER] approve Stage B mutations, and the PV `Retain` patch** | RB 2: namespace, certificate (wait Ready), `helm install vault … -f infra/kubernetes/vault/values-gke.yaml` (only that file; it carries the disabled PDB), NetworkPolicy, `vault-admin` SA; PVCs Bound; **`Retain` on both PVs before G3** | pod Running; PVCs Bound; PVs `Retain`; `kubectl -n ewallet-vault get pdb` returns none; Services ClusterIP only; `$V status` over HTTPS with the published CA: `initialized: false` | Certificate or TLS error: `helm uninstall vault` (no data yet), fix, retry |
| **G3 Initialise** | **[OWNER-ONLY]** | RB 3: `$V init --keys-out $KEYDIR/vault-keys.json`; copy the file to the two offline media | `initialized: true`; key file 0600 outside the repo | Already initialised: **stop**, never initialise again |
| **G4 Auth and policies** | **[OWNER] unseal** | RB 4: `unseal`, `status`, `configure --keys-file`, `operator-token`, `retire-root`, then the root probe `configure --keys-file` | `sealed: false`; `configure` lists `audit, kv, kubernetes-auth`, the policies (`eso-ewallet-agents, eso-ml-platform, feature-api, fraud-api, root-generation, secret-operator`) and roles (`eso-*`, `feature-api`, `fraud-api`, `vault-admin`); `operator_token_created: true, orphan: true`; `root_token_revoked: true`; the root probe fails with `returned 403`. No secret is read or written in G4 | Partial `configure`: re-run it (idempotent) before retiring root |
| **G5 Seed Redis credential** | **[OWNER] approve the copy** | RB 5: pipe `feast-online-redis` → `$V put --token-file $OP --path feature-api/feast-redis --key connection_string --stdin`; `$V versions --token-file $OP --path feature-api/feast-redis`; compare the Secret's resourceVersion with G1 | `put` returns `version: 1`; `versions` (operator token) shows `current_version: 1` and a time, no value; `feast-online-redis` resourceVersion unchanged; Redis untouched | Missing or empty Secret, or `put` refused: stop |
| **G6 ESO** | included in G2 | RB 6: `vault-ca` ConfigMap (create, never apply over an existing one), ESO namespace, `helm install external-secrets … --wait`; `kubectl get clustersecretstore,clusterexternalsecret,pushsecret -A` | 3 deployments Ready; CRDs installed; none of those cluster-scoped objects | Install failed: `helm uninstall external-secrets` (nothing depends on it yet) |
| **G7 Shadow sync** | included | RB 7: `kubectl create -f infra/kubernetes/external-secrets/ml-platform.yaml`; wait SecretStore and ExternalSecret Ready; `shadow_compare.sh` | `SHADOW_MATCH`, exit 0; `feast-online-redis` resourceVersion unchanged | Any other status: stop; the consumer is unchanged |
| **G8 Consumer switch** | **[OWNER] approve the production release change** | `$P --mode consumer-switch` (must be READY); record the release revision; RB 8 `helm upgrade ewallet-serving … --reuse-values -f values-vault-eso.yaml` | rollout complete; Deployment references `feature-api-redis-vault`; no `EWALLET_VAULT_*` | Preflight not READY: do not upgrade. Readiness fails: RB 9 |
| **G9 Business E2E** | included | the §4 lookup again, same fixture | verdict class per §4 | `REGRESSION_AFTER_MIGRATION`: RB 9 rollback, then stop |
| **G10 Negative tests** | **[OWNER] approve the test objects** | §5 procedure | expected rejection reasons; no target Secret; no existing object touched | Any path readable that should not be: **stop**, RB 9, investigate |
| **G11 Rollback drill** | **[OWNER] approve** | RB 9 rollback → lookup → RB 8 again → lookup | both lookups in the G1 class; Vault PVCs untouched | — |
| **G12 Sleep** | **[OWNER] approve the guarded sleep, KEDA pause, and each `--also-approve` target for this `$CID`** | §6 sequence | `verify --expect sleep` passes; 0 VMs; Vault and ESO still installed; both PVCs Bound, PVs `Retain`; no LoadBalancer created | `SLEEP_BLOCKED`: report it; never add flags or targets without approval |

An approved restart test (sealed → manual unseal → sync resumes) is optional evidence (E7). It
needs its own owner approval and must never be done by rotating the real Redis password.

## 4. Feature API business lookup

**Fixture.** [`lookup-request.json`](../../evidence/edai2/keda/g5b_20261107T204930Z/validation/smoke/lookup-request.json)
from G5-B: real entity IDs from the Gold data, `event_timestamp` 2026-06-01T00:00:00Z, and 5
feature names. A lookup only reads Redis; it never writes. Assessment:

- On 2026-11-07 it returned HTTP 200 with all 5 features (oldest feature timestamp 2026-05-31T23:59:56Z).
- The 424 `missing_feature` that G3-A1 saw on 2026-11-21 came from a deliberately synthetic body
  with made-up IDs, not from this fixture.
- The online store (`feature-store/redis`) is a StatefulSet with `appendonly yes` on a 2 GiB PVC,
  so the materialized rows should survive sleeps.
- **It cannot be confirmed while the cluster is asleep.** No materialization or other write is part of this campaign.

**Call (G1, G9, G11)**, through a local port-forward, with the body saved in the campaign directory:

```bash
kubectl --context $CTX -n ml-platform port-forward svc/feature-api 18080:80 &
curl -s -o lookup.json -w '%{http_code}\n' -H 'content-type: application/json' \
  --data-binary @docs/evidence/edai2/keda/g5b_20261107T204930Z/validation/smoke/lookup-request.json \
  http://127.0.0.1:18080/v1/features/lookup
jq -c '{features: (.features | keys), oldest_feature_timestamp, error}' lookup.json
```

**G9 verdict classes** (exactly one; HTTP 424 is never a business success):

| Class | Condition |
|---|---|
| `BUSINESS_LOOKUP_PASS` | G1 and G9 both return 200 with the same 5 feature names and a valid body (`features`, `oldest_feature_timestamp`, `correlation_id`) |
| `SECRET_INTEGRATION_PASS_BUSINESS_LOOKUP_UNAVAILABLE` | G1 already returned 424, or another non-200 caused by data rather than credentials. G9 returns the same code, and `/health/ready` (which opens Redis with the credential) passes on the switched pod. The Secret integration is proven; a business lookup is not claimed |
| `REGRESSION_AFTER_MIGRATION` | G9 differs from G1 for the worse (for example 200 → 424/500, readiness fails, Redis auth errors). Roll back (RB 9) and stop |

## 5. G10 negative tests: no overwrite, no residue

All test objects are created, never applied, with label `ewallet.io/vault-campaign=$CID`, names
prefixed `vault-neg-`, and synthetic values only. Each run deletes its objects by label afterwards.

1. **Precheck (READ-ONLY)** that none of these names exist:
   - in `ml-platform`: `externalsecret/vault-neg-agent-path`, `secretstore/vault-neg-wrong-sa`,
     `secretstore/vault-neg-wrong-aud`, and the Secrets with the same names;
   - in `ewallet-agents`: `serviceaccount/eso-vault-reader`, `configmap/vault-ca`, `secretstore/vault`,
     `externalsecret/agentgateway-client-key-vault`, `externalsecret/vault-neg-feature-path`,
     `secret/agentgateway-client-key-vault`, `secret/vault-neg-feature-path`.
   Any hit: stop. Existing Agent resources are never changed.
2. Seed the staged agent path with a **new synthetic** key (RB 5, `openssl rand`), never the live
   gateway key. Then create `ewallet-agents.yaml` and the `vault-ca` ConfigMap in `ewallet-agents`
   with `kubectl create`, adding the campaign label.
3. Cases and expected causes (`kubectl -n external-secrets logs deploy/external-secrets`):

   | Case | Expected cause |
   |---|---|
   | `ml-platform` ExternalSecret for `agent-platform/agentgateway-client-key` | `permission denied` |
   | `ewallet-agents` ExternalSecret for `feature-api/feast-redis` | `permission denied` |
   | store in `ml-platform` with the `feature-api` SA | `service account name not authorized` |
   | store in `ml-platform` with audience `kubernetes` | `invalid audience` |
   | `kubectl -n ewallet-vault get svc` | ClusterIP only |

   Each must leave no target Secret.
4. Cleanup:
   `kubectl -n ml-platform delete externalsecret,secretstore -l ewallet.io/vault-campaign=$CID`,
   then the same in `ewallet-agents`, plus its `serviceaccount,configmap`. Keep the staged
   `ewallet-agents` store only if the owner wants it as evidence; it is opt-in.

## 6. Sleep (G12) and early stop

The approved guarded-sleep workflow, as used by the G3-B campaigns:

```bash
python3 ops/gcp/cloud_lab.py record-manifest --campaign $CID --out ~/ewallet-evidence/vault/$CID/recovery-manifest
python3 ops/gcp/cloud_lab.py plan-sleep --manifest docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z
# guarded_preparation.not_covered lists PDBs outside the approved record; earlier campaigns: ewallet-mcp/feature-rag-mcp
CLOUD_LAB_APPROVED=yes CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:$CID CLOUD_LAB_KEDA_PAUSE_APPROVAL=ewallet-dev:$CID \
  python3 ops/gcp/cloud_lab.py sleep --scale-pdb-guarded --pause-keda --manifest docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z \
  --campaign $CID --also-approve <each not_covered target the owner approved for this $CID> --execute --confirm-cluster ewallet-dev
python3 ops/gcp/cloud_lab.py verify --expect sleep --manifest ~/ewallet-evidence/vault/$CID/recovery-manifest
kubectl --context $CTX -n ewallet-vault get pvc; kubectl --context $CTX get pv | grep vault-0     # Bound; Retain
```

Each `--also-approve` target needs the owner's authorization for this `$CID`; an earlier campaign's approval does not carry over. Vault has no PDB, so it never appears in `not_covered`. The sleep keeps Vault, ESO and their PVCs installed.

**Early stop:** record sanitized observations; keep `feast-online-redis` untouched; roll the
consumer back if it was switched (RB 9); keep Vault and its PVCs; leave ESO unless it was never
used (RB *Recovery*); then ask for the sleep above. Never leave the cluster awake because a step failed.

## 7. Evidence checklist (owner screenshots)

Saved in `~/Pictures/Screenshots/`. Accepted files go to `docs/evidence/edai2/vault/gke_$CID/screenshots/`
with `sha256sum *.png > SHA256SUMS`. A screenshot must never show an unseal share, token, private key, Secret value or full environment.

| ID | File | Mandatory | Gate | Command / place | Must show | Doc placement |
|---|---|---|---|---|---|---|
| E1 | `vault_runtime.png` | yes | G2/G4 | `kubectl -n ewallet-vault get pods,pvc,svc,pdb` and `$V status` | `vault-0` Running, 2 PVCs Bound, ClusterIP only, no PDB, 2.0.4, `initialized true`, `sealed false` | VAULT.md *Architecture* |
| E2 | `vault_auth_policies.png` | yes | G4 + G5 | G4 `configure` output (policies and roles), `cat infra/kubernetes/vault/policies/eso-ml-platform.hcl`, and the G5 `versions --token-file $OP` output | roles incl. `eso-ml-platform` and `vault-admin`, the one-path read policy, `current_version: 1` without a value | VAULT.md *Kubernetes authentication, roles and policies* |
| E3 | `eso_runtime.png` | yes | G6 | `kubectl -n external-secrets get deploy,pods` | controller, webhook, cert-controller Ready | VAULT.md *External Secrets Operator* |
| E4 | `eso_secret_sync.png` | yes | G7 | `kubectl -n ml-platform get secretstore,externalsecret`; `kubectl -n ml-platform get secret feature-api-redis-vault -o json \| jq '.data \| keys'`; `shadow_compare.sh …` | `Valid`/`SecretSynced`, key `connection_string`, `SHADOW_MATCH` | VAULT.md *SecretStore and ExternalSecret* |
| E5 | `vault_feature_api_e2e.png` | yes | G8/G9 | the `secretKeyRef` jsonpath (RB 8) and the §4 lookup | `feature-api-redis-vault`, pod Ready, lookup result and the G9 verdict class | VAULT.md *Feature API integration* |
| E6 | `vault_access_denied.png` | yes | G10 | negative ExternalSecret status, the controller log line, and `kubectl get secret <target>` NotFound | `permission denied`, no target Secret | VAULT.md *Namespace isolation* |
| E7 | `vault_secret_lifecycle.png` | optional | G11 | after an approved restart: `$V status` sealed → `unseal` → ExternalSecret `SecretSynced` | manual unseal and resumed sync | VAULT.md *Rotation and recovery* |
| E8 | `vault_guarded_sleep.png` | yes | G12 | `verify --expect sleep` output; `kubectl -n ewallet-vault get pvc`; `kubectl get pv \| grep vault-0` | 0 nodes, PVCs Bound, PVs `Retain`, no new LoadBalancer | VAULT.md *Operational limitations* |

## 8. Documentation plan

| Document | After an accepted campaign |
|---|---|
| [VAULT.md](../../edai2/platform/VAULT.md) | `GKE_DEPLOYED` / `GKE_RUNTIME_VERIFIED` only for gates that passed; GKE environment; E1–E8 references; the G9 verdict class; limitations |
| [VAULT_ESO_RUNBOOK.md](VAULT_ESO_RUNBOOK.md) | corrections found during the run |
| `docs/evidence/edai2/vault/gke_$CID/CAMPAIGN_REPORT.md` | new: campaign ID, commit, environment, approved scope (incl. `--also-approve` targets), resource inventory, gate results, issues, rollback, evidence with SHA-256, final state, limitations. It follows the existing `docs/evidence/edai2/gateway/*/CAMPAIGN_REPORT.md` |
| `docs/edai2/RUBRIC_EVIDENCE_INDEX.md` row 54 | changed only after the owner accepts the evidence |

## 9. Resources and cost

| Component | Requests | Limits |
|---|---|---|
| Vault (1 pod) | 100m CPU, 256 Mi | 500m, 512 Mi |
| ESO controller | 20m, 64 Mi | 200m, 256 Mi |
| ESO webhook | 10m, 32 Mi | 100m, 128 Mi |
| ESO cert-controller | 10m, 32 Mi | 100m, 128 Mi |
| **Total** | **140m CPU, 384 Mi** | 900m, 1 Gi |
| Disks | 5 GiB data + 1 GiB audit, pd-balanced (`standard-rwo`), zone b | — |

- No new node pool, LoadBalancer, IP address, DNS record, KMS key or IAM binding.
- The extra requests fit on the usual 5 nodes (≈19.6 vCPU and 64 GiB allocatable, ≈10.3 vCPU requested).
- The 6 GiB disk costs roughly US$0.6 per month, awake or asleep (published pd-balanced list price
  about US$0.10–0.12 per GiB-month); with `Retain`, a deleted PVC's disk keeps costing until it is deleted.
- Awake cost is the usual node cost for the campaign (G1–G12 ≈ 2–3 h).

## 10. Owner-only actions

| When | Action |
|---|---|
| Before G1 | Approve Stage B and the wake |
| G2 | Approve the Kubernetes mutations and the PV `Retain` patch |
| G3 | Run `init` personally; store `vault-keys.json` on two offline encrypted media; never paste shares or tokens into chat or screenshots |
| G4 and every later wake | Unseal personally |
| G5 | Approve copying the existing Redis credential into Vault |
| G8 | Approve the production release change |
| G10–G11 | Approve the negative-test objects and the rollback drill; optionally the restart test (E7) |
| G1–G12 | Take screenshots E1–E8 |
| G12 | Approve the guarded sleep, the KEDA pause and each `--also-approve` target for this campaign |
| After | Accept the evidence before any rubric row changes |
