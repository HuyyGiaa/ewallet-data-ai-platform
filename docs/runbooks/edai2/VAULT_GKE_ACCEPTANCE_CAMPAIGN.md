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
| Latest recovery manifest | `~/ewallet-evidence/gateway/g3b-full-20261122T181805Z/recovery-manifest` (73 workloads, ≈10.3 vCPU and 24.8 GiB of requests, 5 nodes) |
| Read-only preflight | [`gke_preflight_20261124T055210Z`](../../evidence/edai2/vault/gke_preflight_20261124T055210Z/gates.jsonl): `VAULT_GKE_PREFLIGHT_READY`; `cert-manager-ready` NOT_CHECKED (asleep) |

### Preflight gates

Statuses are PASS, FAIL, BLOCKED or NOT_CHECKED. *Live* means a read-only query against the cluster.

| Gate | Expected | Observed | Status | Required action |
|---|---|---|---|---|
| Git baseline | `feature/edai2-vault-eso`, gateway branch as ancestor, no merges from main, clean tree | as expected | PASS | — |
| Cluster reachable (live) | API answers on the pinned context | v1.36.4-gke.2377000 | PASS | — |
| No earlier Vault/ESO (live) | no `ewallet-vault`/`external-secrets` namespace, ESO CRD, release or Vault PVC | none | PASS | none; nothing to adopt or re-initialise |
| cert-manager installed (live) | Certificate and Issuer CRDs | v1.16.2 release, CRDs present | PASS | — |
| cert-manager running | 3 deployments available | 0 nodes | NOT_CHECKED | G1: rerun the preflight after the wake |
| cert-manager → Vault TLS → ESO chain | `certificate.yaml` issues a CA and a server certificate that names `vault.ewallet-vault.svc.cluster.local`; ESO verifies it | verified on kind with cert-manager v1.16.2: [`eso_local_20261124T055021Z`](../../evidence/edai2/vault/eso_local_20261124T055021Z/results.jsonl), 65/65, TLS mode `cert-manager` | PASS (kind) | G2 repeats it on GKE |
| StorageClass (live) | `standard-rwo`, PD CSI, WaitForFirstConsumer | as expected; **reclaim Delete** | PASS | G2: optionally set the two PVs to `Retain` (owner) |
| Disk zone vs pool | zonal disks schedulable after a wake | pool is single-zone (b); disks are created in b | PASS | — |
| Vault drain behaviour | no PDB that blocks a resize to zero | chart default `maxUnavailable: 0` found; disabled in `values-gke.yaml`; checked on kind | PASS (fixed in source) | — |
| Redis Secret (live) | `ml-platform/feast-online-redis` with non-empty `connection_string` | present; no owner or labels (created out of band 2026-10-31) | PASS | copy it only, never rotate it (several clients) |
| Feature API release (live) | one release owns the Deployment | `ewallet-serving` rev 3 in `ml-platform`. It also owns the Feature API SA and Services, `fraud-api-metrics`, the ScaledObjects `feature-api` and `fraud-api`, and 3 NetworkPolicies | PASS | — |
| Release drift (live) | live manifest == repository chart rendered with the release's values | identical | PASS | — |
| Overlay scope (live) | overlay changes only the Secret name | exactly one line: `feast-online-redis` → `feature-api-redis-vault` | PASS | G8 re-renders against the then-live release |
| Admission webhooks | API server can reach the ESO webhook | public-node cluster; the cert-manager webhook already works the same way | PASS (static) | G6 checks the webhook Ready |
| Business lookup baseline | a lookup returns features with the current Secret | G5-B (2026-11-07) fixture: 200 with 5 features; G3-A1 (2026-11-21) synthetic lookup: 424 `missing_feature` | NOT_CHECKED | G1 decides whether G9 can show a business success |
| Capacity | Vault + ESO fit on the woken pool | +0.14 vCPU, +384 MiB requests on ≈19.6 vCPU / 64 GiB allocatable | PASS (estimate) | — |

## 2. Risks

| Class | Risk | Handling |
|---|---|---|
| IMPORTANT | The CI identity `ewallet-ci@…` holds `roles/container.developer`, which includes `container.serviceAccounts.createToken`. CI could mint a `vault-admin` token and start or cancel a root-generation ceremony (never finish one without 2 shares), and could mint `eso-vault-reader` tokens to read the paths that reader may read. Cluster-admin (owner) and, after G6, the ESO controller can do the same. | Accepted for the lab and documented. Removing the permission is an IAM change outside this campaign (owner decision) |
| IMPORTANT | `standard-rwo` deletes a disk when its PVC is deleted | Never delete the namespace or PVCs; optional G2 step sets both PVs to `Retain` |
| IMPORTANT | The online store may hold no row for the fixture entity (G3-A1 saw 424) | G1 baseline lookup decides. If it returns 424, G9 reports readiness plus `APPLICATION_REACHED` only; a business success needs an owner-approved materialization, which is outside this campaign |
| IMPORTANT | GKE may auto-upgrade nodes mid-campaign (REGULAR channel, no maintenance window) | An eviction restarts Vault sealed: unseal again. Consumers keep their Secrets |
| OPTIONAL | Vault keeps its certificate until restart; cert-manager renews it at day 75 | Each wake restarts Vault; RB step 12 has the check |
| OPTIONAL | `cloud_lab.py evaluate_pdbs` ignores `maxUnavailable: 0` budgets | Not triggered (no such PDB in the cluster; Vault's is disabled). A tool fix is a separate change |

## 3. Campaign gates (Stage B)

Campaign ID `vault-gke-<UTC timestamp>`. Working directory outside the repository:
`~/ewallet-evidence/vault/<campaign>/` (mode 700). Recovery material goes in `~/ewallet-evidence/vault/private/` only.

Throughout: `export KUBECONFIG=~/.cache/ewallet-cloud-lab/kubeconfig`, plus `--context $CTX` and `--kube-context $CTX` on every command.

| Gate | Owner approval | Preconditions | Actions (RB = runbook step) | Pass when | Stop / rollback |
|---|---|---|---|---|---|
| **G1 Wake and baseline** | **[OWNER] approve the wake** | Stage A report reviewed | `CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest <latest recovery manifest> --nodes 5 --confirm-cluster ewallet-dev --execute`; `cloud_lab.py verify --expect awake --manifest …`; rerun `scripts/vault/gke_preflight.py`; baseline lookup with the existing Secret: port-forward `svc/feature-api` 18080:80 and POST `docs/evidence/edai2/keda/g5b_20261107T204930Z/validation/smoke/lookup-request.json` to `/v1/features/lookup`; record `helm -n ml-platform history ewallet-serving`, the Deployment's `secretKeyRef`, and the Gateway releases' revisions (`ewallet-edge` 21, `ewallet-edge-gateway` 16) | preflight READY with `cert-manager-ready` PASS; workloads healthy; lookup result recorded (200 with features, or 424) | Unhealthy platform, drift, or preflight BLOCKED: stop and sleep (§6) |
| **G2 Vault infrastructure** | **[OWNER] approve Stage B mutations** | G1 PASS | RB 2: namespace, certificate (wait Ready), `helm install vault`, NetworkPolicy, `vault-admin` SA; PVCs Bound; optional PV `Retain` | pod Running; PVCs Bound; all Vault Services ClusterIP; no PDB in `ewallet-vault`; `$V status` answers over HTTPS with the published CA (`initialized: false`) | Certificate not Ready or TLS error: `helm uninstall vault` (data is empty), fix, retry |
| **G3 Initialise** | **[OWNER-ONLY]** | G2 PASS; `KEYDIR` mode 700; two offline media | RB 3: `$V init --keys-out $KEYDIR/vault-keys.json`, then copy the file to the two offline media | `initialized: true`; file mode 0600 outside the repo | Already initialised: **stop**, never initialise again. Lost file: see RB *Recovery* |
| **G4 Auth and policies** | **[OWNER] unseal** | G3 PASS | RB 4: `unseal`, `status` (`sealed: false`), `configure`, `operator-token`, `retire-root`, then check that the retired root is rejected | applied list contains `audit, kv, kubernetes-auth`, both `eso-*` roles and `vault-admin`; root rejected; operator token answers `versions` | Partial `configure`: re-run it (idempotent) |
| **G5 Seed Redis credential** | **[OWNER] approve the copy** | G4 PASS | RB 5: pipe `feast-online-redis` → `$V put --token-file $OP --path feature-api/feast-redis --key connection_string --stdin`; then `$V versions` | `current_version: 1`; Redis untouched (no restart, no password change) | Missing or empty Secret: stop |
| **G6 ESO** | included in G2 approval | G5 PASS | RB 6: `vault-ca` ConfigMap, ESO namespace, `helm install external-secrets … --wait`; `kubectl get clustersecretstore,clusterexternalsecret,pushsecret -A` | 3 deployments Ready; CRDs installed; no cluster-scoped stores or PushSecrets | Install failed: `helm uninstall external-secrets` (nothing depends on it yet) |
| **G7 Shadow sync** | included | G6 PASS | RB 7: apply `ml-platform.yaml`; wait SecretStore and ExternalSecret Ready; `scripts/vault/shadow_compare.sh $CTX ml-platform feast-online-redis feature-api-redis-vault` | `SHADOW_MATCH`, exit 0; `feast-online-redis` resourceVersion unchanged | Any other status: stop. Fix and re-sync; the consumer is unchanged |
| **G8 Consumer switch** | **[OWNER] approve the production release change** | G7 PASS | rerun the preflight (`serving-no-drift`, `serving-overlay-scope` PASS against the live release); record the revision; RB 8 `helm upgrade ewallet-serving … --reuse-values -f values-vault-eso.yaml` | rollout complete; Deployment references `feature-api-redis-vault`; no `EWALLET_VAULT_*` variable | Readiness fails: RB 9 rollback |
| **G9 Business E2E** | included | G8 PASS | the G1 lookup again, with the same fixture | If G1 got 200: 200 with the same feature names, schema valid. If G1 got 424: readiness and `APPLICATION_REACHED` only, and no business success claimed | Regression against G1: rollback (RB 9) |
| **G10 Negative tests** | included | G7 PASS | labelled ExternalSecrets in `ml-platform` for `agent-platform/agentgateway-client-key` and in `ewallet-agents` for `feature-api/feast-redis` (needs the staged `ewallet-agents.yaml`, synthetic value only); a store using the `feature-api` SA; a store with audience `kubernetes`; check each in `kubectl -n external-secrets logs deploy/external-secrets`; check `kubectl get svc -n ewallet-vault` has no LoadBalancer; delete each test object right after | `permission denied`, `service account name not authorized`, `invalid audience`; no target Secret | A path readable that should not be: **stop**; roll the consumer back; investigate |
| **G11 Rollback drill** | **[OWNER] approve** | G9 PASS | RB 9 rollback, then lookup; then RB 8 again, then lookup | both lookups as in G9; Vault PVCs untouched | — |
| **G12 Sleep integration** | **[OWNER] approve the sleep** (guarded, KEDA, `--also-approve ewallet-mcp/feature-rag-mcp` as in earlier campaigns) | G1–G11 done | `cloud_lab.py record-manifest --campaign <id> --out <dir>`; `plan-sleep`; the approved sleep; `verify --expect sleep`; `kubectl -n ewallet-vault get pvc` | 0 VMs; Vault and ESO still installed; both PVCs Bound; no LoadBalancer created | Sleep blocked: report `SLEEP_BLOCKED`; never use tool-gated flags without approval |

An approved restart test (sealed → manual unseal → sync resumes) is optional evidence (E7).
It needs a separate owner approval at G11 time. It must never be done by rotating the real Redis password.

## 4. Evidence checklist (owner screenshots)

The owner saves screenshots in `~/Pictures/Screenshots/`. Accepted files go to
`docs/evidence/edai2/vault/gke_<campaign>/screenshots/` with `sha256sum *.png > SHA256SUMS`.
A screenshot must never show an unseal share, token, private key, Secret value or full environment.

| ID | File | Mandatory | Gate | Command / place | Must show | Doc placement |
|---|---|---|---|---|---|---|
| E1 | `vault_runtime.png` | yes | G2/G4 | `kubectl -n ewallet-vault get pods,pvc,svc` and `$V status` | `vault-0` Running, 2 PVCs Bound, ClusterIP services only, version 2.0.4, `initialized true`, `sealed false` | VAULT.md *Architecture* |
| E2 | `vault_auth_policies.png` | yes | G4 | `$V configure` output (from G4) next to `cat infra/kubernetes/vault/policies/eso-ml-platform.hcl`; `$V versions --token-file $OP --path feature-api/feast-redis` | applied roles incl. `eso-ml-platform`, the one-path read policy, KV v2 metadata without a value | VAULT.md *Kubernetes authentication, roles and policies* |
| E3 | `eso_runtime.png` | yes | G6 | `kubectl -n external-secrets get deploy,pods` | controller, webhook, cert-controller Ready | VAULT.md *External Secrets Operator* |
| E4 | `eso_secret_sync.png` | yes | G7 | `kubectl -n ml-platform get secretstore,externalsecret`; `kubectl -n ml-platform get secret feature-api-redis-vault -o json \| jq '.data \| keys'`; `shadow_compare.sh …` | `Valid`/`SecretSynced`, key `connection_string`, `SHADOW_MATCH` | VAULT.md *SecretStore and ExternalSecret* |
| E5 | `vault_feature_api_e2e.png` | yes | G8/G9 | the `secretKeyRef` jsonpath (RB 8) and the lookup response | `feature-api-redis-vault`, pod Ready, lookup result as in G9 | VAULT.md *Feature API integration* |
| E6 | `vault_access_denied.png` | yes | G10 | negative ExternalSecret status plus the controller log line, and `kubectl get secret <target>` NotFound | `permission denied`, no target Secret | VAULT.md *Namespace isolation* |
| E7 | `vault_secret_lifecycle.png` | optional | G11 | after an approved restart: `$V status` sealed → `unseal` → ExternalSecret `SecretSynced` again | manual unseal and resumed sync | VAULT.md *Rotation and recovery* |
| E8 | `vault_guarded_sleep.png` | yes | G12 | `cloud_lab.py verify --expect sleep …` and `kubectl -n ewallet-vault get pvc` | 0 nodes, PVCs Bound and retained, no new LoadBalancer | VAULT.md *Operational limitations* |

## 5. Documentation plan

| Document | After an accepted campaign |
|---|---|
| [VAULT.md](../../edai2/platform/VAULT.md) | status `GKE_DEPLOYED` / `GKE_RUNTIME_VERIFIED` only for gates that passed; GKE environment; screenshot references E1–E8; known limitations |
| [VAULT_ESO_RUNBOOK.md](VAULT_ESO_RUNBOOK.md) | corrections found during the run |
| `docs/evidence/edai2/vault/gke_<campaign>/CAMPAIGN_REPORT.md` | new: campaign ID, commit, environment, approved scope, resource inventory, gate results, issues, rollback, evidence list with SHA-256, final state, limitations. It follows the existing `docs/evidence/edai2/gateway/*/CAMPAIGN_REPORT.md` |
| `docs/edai2/RUBRIC_EVIDENCE_INDEX.md` row 54 | changed only after the owner accepts the evidence |

## 6. If the campaign stops early

1. Record sanitized observations in the campaign directory.
2. Keep `feast-online-redis` untouched. If the consumer was switched, roll it back (RB 9).
3. Keep Vault and its PVCs. Leave ESO installed unless it was never used (RB *Recovery*).
4. Ask the owner to approve the usual sleep (G12). Never leave the cluster awake because a step failed.

## 7. Resources and cost

| Component | Requests | Limits |
|---|---|---|
| Vault (1 pod) | 100m CPU, 256 Mi | 500m, 512 Mi |
| ESO controller | 20m, 64 Mi | 200m, 256 Mi |
| ESO webhook | 10m, 32 Mi | 100m, 128 Mi |
| ESO cert-controller | 10m, 32 Mi | 100m, 128 Mi |
| **Total** | **140m CPU, 384 Mi** | 900m, 1 Gi |
| Disks | 5 GiB data + 1 GiB audit, pd-balanced (`standard-rwo`), zone b | — |

- No new node pool, LoadBalancer, IP address, DNS record, KMS key or IAM binding.
- The extra requests fit on the usual 5 nodes.
- The 6 GiB of balanced Persistent Disk cost roughly US$0.6 per month (about US$0.001 per hour,
  at the published pd-balanced list price of about US$0.10–0.12 per GiB-month), awake or asleep.
  That is small next to the ≈US$0.107 per hour asleep baseline.
- Awake cost is the usual node cost for the campaign's duration (G1–G12 ≈ 2–3 h).

## 8. Owner-only actions

| When | Action |
|---|---|
| Before G1 | Approve Stage B and the wake |
| G2 | Approve the Kubernetes mutations, and optionally the PV `Retain` patch |
| G3 | Run `init` personally; store `vault-keys.json` on two offline encrypted media; never paste shares or tokens into chat or screenshots |
| G4 and every later wake | Unseal personally |
| G5 | Approve copying the existing Redis credential into Vault |
| G8 | Approve the production release change (consumer switch) |
| G10–G11 | Approve the negative-test objects and the rollback drill; optionally the restart test (E7) |
| G1–G12 | Take screenshots E1–E8 |
| G12 | Approve the guarded sleep |
| After | Accept the evidence before any rubric row changes |
