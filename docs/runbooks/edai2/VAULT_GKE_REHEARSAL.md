# Vault + ESO GKE technical rehearsal (Campaign A; prepared, not executed)

A temporary, synthetic deployment of the Vault + External Secrets Operator implementation
([VAULT.md](../../edai2/platform/VAULT.md)) on `ewallet-dev`, run by Claude after the owner
approves it. Its job is to find GKE-specific problems before the owner's official acceptance
([VAULT_GKE_ACCEPTANCE_CAMPAIGN.md](VAULT_GKE_ACCEPTANCE_CAMPAIGN.md), Campaign B). It ends with
everything removed and the cluster asleep.

**Status: prepared and dry-run on kind; not executed on GKE.** Nothing on this page runs
without the owner's explicit approval, and the approval names one rehearsal ID.

| | Campaign A: technical rehearsal (this page) | Campaign B: official acceptance |
|---|---|---|
| Runs it | Claude, after owner approval | the owner, from the validated runbook |
| Secrets | synthetic only, destroyed at cleanup | the real Redis credential, copied from `feast-online-redis` |
| Consumer | a redis-cli Job against a synthetic Redis in namespace `vault-rehearsal` | the production Feature API (`ewallet-serving`) |
| Vault data | disks deleted at cleanup (reclaim `Delete`) | disks kept (`Retain`), Vault and ESO stay installed |
| End state | as before the rehearsal; cluster asleep | Vault + ESO live, Feature API on `feature-api-redis-vault`; cluster asleep |
| Evidence | machine-readable results only | owner screenshots E1–E9 and results |

## 1. What it proves, and what it cannot

On real GKE, with the files the official campaign will use: cert-manager issuance, the official
Vault and ESO charts, Persistent Disk provisioning and deletion, Kubernetes auth through GKE's
TokenReview, ESO's token requests with audience `vault`, Calico enforcement of the Vault
NetworkPolicy, image pulls by GKE nodes, the seal/unseal behaviour across a restart, the
cleanup of every cluster-scoped object, and that the shared platform is unchanged afterwards.

It cannot prove the production consumer switch, the real credential's `SHADOW_MATCH` or the
business lookup: those touch production and belong to Campaign B (G5–G11).

## 2. Isolation design (R1)

**Decision: Vault and ESO under their official names; the consumer side in a temporary namespace.**

| Choice | Why |
|---|---|
| Vault in `ewallet-vault`, release `vault`; ESO in `external-secrets`, release `external-secrets` | These are the files under test: the certificate SANs, NetworkPolicy, values and SecretStore address all name `ewallet-vault`. Renaming them would rehearse different manifests. The preflight proves none of these exist on GKE (2026-11-24, [`gke_preflight_20261124T165422Z`](../../evidence/edai2/vault/gke_preflight_20261124T165422Z/gates-preinstall.jsonl)), so everything under these names afterwards belongs to the rehearsal |
| ESO's CRDs and cluster RBAC are the official ones | Unavoidable: ESO's CRDs are cluster-scoped and there can be only one installation. None exists on GKE, and nothing else uses them |
| Consumer side only in `vault-rehearsal` | No object is created in `ml-platform`, `ewallet-agents`, `feature-store` or any other existing namespace. The rehearsal's Vault role `eso-vault-rehearsal` is bound to `vault-rehearsal/eso-vault-reader` and reads one synthetic path, `feature-api/rehearsal-redis` ([fixture](../../../scripts/vault/rehearsal/)) |
| Synthetic Redis, not `feature-store/redis` | The consumer authenticates against a Redis whose password comes from a separate Secret, as the real one does. A wrong value fails (`AUTH_FAILED`), so success is a real authentication, not Pod readiness |
| Official bootstrap applied too | `configure` is rehearsed exactly as G4. The official roles then exist in the rehearsal Vault but nothing is seeded under their paths |
| Ownership label | every object the script creates carries `ewallet.io/vault-rehearsal=<ID>`, set at creation with `kubectl create`; cleanup refuses a namespace without it |

The script never modifies [`eso_local_validate.sh`](../../../scripts/vault/eso_local_validate.sh)
or bypasses its kind-only guard: it is a separate driver,
[`gke_rehearsal.sh`](../../../scripts/vault/gke_rehearsal.sh), that reuses the same assertion classes.

### Resource inventory

| Resource | Scope | Stateful | Effect on existing workloads | Removed by cleanup |
|---|---|---|---|---|
| Namespaces `ewallet-vault`, `external-secrets`, `vault-rehearsal` | cluster | — | none (new) | yes (label-checked) |
| cert-manager `Issuer`/`Certificate` ×2 each, Secrets `vault-ca`, `vault-server-tls` | `ewallet-vault` | — | uses the existing cert-manager | with the namespace |
| Release `vault`: StatefulSet `vault` (1 pod), Services (ClusterIP), ConfigMap, SA | `ewallet-vault` | — | ≈100m CPU, 256 Mi | `helm uninstall` |
| ClusterRoleBinding `vault-server-binding` (`system:auth-delegator`, for TokenReview) | cluster | — | none | `helm uninstall` |
| PVCs `data-vault-0` 5 GiB, `audit-vault-0` 1 GiB → 2 pd-balanced disks, zone b | `ewallet-vault` / project | **yes (synthetic)** | ≈ US$0.02/day | PVC delete, reclaim `Delete` deletes the disks; checked with `gcloud compute disks describe` |
| NetworkPolicy `vault-clients`, SA `vault-admin` | `ewallet-vault` | — | admits `ml-platform` Feature/Fraud API pods to the rehearsal Vault, which they do not use | with the namespace |
| Release `external-secrets`: 3 Deployments | `external-secrets` | — | ≈40m CPU, 128 Mi | `helm uninstall` |
| 25 ESO CRDs, 5 ClusterRoles, 2 ClusterRoleBindings, 2 ValidatingWebhookConfigurations | cluster | — | `external-secrets-view`/`-edit` aggregate into the built-in `view`/`edit` roles while installed; the webhooks match only ESO objects | `helm uninstall` (no `keep` annotation); checked by `verify-clean` |
| ConfigMap `vault-ca`, SA `eso-vault-reader`, `SecretStore`, `ExternalSecret`, Secret `rehearsal-redis-vault` (ESO-owned), Secret `rehearsal-redis-server`, Deployment/Service `redis`, consumer Jobs, negative-test objects | `vault-rehearsal` | in-memory Redis only | none | with the namespace |
| Synthetic unseal shares, root and operator tokens, CA copy | laptop, `$DIR/private` (mode 700) | — | — | `rm -rf` at cleanup |

Existing resources the rehearsal only **reads**: Helm release list, `ml-platform/feast-online-redis`
(its `resourceVersion`, and the preflight's non-empty check; the value is never extracted,
printed, stored or copied), `ml-platform/feature-api` (generation and Secret name), Services
(type), `ewallet-ingress/ewallet-edge-gateway` (type), cert-manager deployments, StorageClasses.

### Irreversible steps

- `init` on the rehearsal Vault (synthetic data; irreversible only for that Vault, which cleanup deletes).
- Cleanup's PVC deletion destroys the two synthetic disks (reclaim `Delete`); approved separately.
- Uninstalling ESO deletes its CRDs, and with them every ExternalSecret in the cluster. The
  preflight proves there are none besides the rehearsal's.

## 3. Prerequisites

- Owner approval of this rehearsal and its ID `vault-rehearsal-<UTC yyyymmddThhmmssZ>`.
- Laptop on the approved network; `KUBECONFIG=~/.cache/ewallet-cloud-lab/kubeconfig` (the script refuses any other on GKE).
- Charts in `CHARTS=~/ewallet-evidence/vault/charts` (RB step 1; digests checked by the script).
- `gcloud` authenticated (wake/sleep, disk check); `kubectl`, `helm`, `jq`, `openssl`, `curl`, Python 3 with PyYAML.
- Free RAM on the laptop is not a factor: nothing runs locally besides kubectl.

```bash
ID=vault-rehearsal-$(date -u +%Y%m%dT%H%M%SZ)
export KUBECONFIG=~/.cache/ewallet-cloud-lab/kubeconfig CTX=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export REHEARSAL_CONTEXT=$CTX REHEARSAL_ID=$ID REHEARSAL_DIR=~/ewallet-evidence/vault/$ID CHARTS=~/ewallet-evidence/vault/charts
R="bash scripts/vault/gke_rehearsal.sh"
MANIFEST=~/ewallet-evidence/gateway/g3b-owner-evidence-20261124T083106Z/recovery-manifest   # newest; recheck before use
```

## 4. Procedure

Each script phase writes one JSON line per step to `$REHEARSAL_DIR/results.jsonl` and stops at
the first failed assertion. Mutating phases need `VAULT_REHEARSAL_APPROVED=$ID`; cleanup needs
`VAULT_REHEARSAL_CLEANUP_APPROVED=$ID`.

| Step | Approval | Command | PASS when | Stop / rollback |
|---|---|---|---|---|
| **R0 Read-only preflight (asleep)** | none | `python3 scripts/vault/gke_preflight.py --mode preinstall --kubeconfig $KUBECONFIG --context $CTX --out $REHEARSAL_DIR/preflight-asleep`; `python3 ops/gcp/cloud_lab.py status`; check `$MANIFEST` is the newest `recovery-manifest` | READY (only `cert-manager-ready` NOT_CHECKED); 0 nodes | any FAIL/BLOCKED: stop and report, nothing to undo |
| **R2 Wake and baseline** | **[OWNER] approve the wake for `$ID`** | `CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest $MANIFEST --nodes 5 --confirm-cluster ewallet-dev --execute`; `cloud_lab.py verify --expect awake --manifest $MANIFEST`; then `$R preflight` | verify passes; `preflight`: chart digests, cert-manager available, **no collisions**, `gke_preflight` READY, `baseline.json` recorded | unhealthy platform or any collision: no Vault install; report, then R10 after approval |
| **R3 Temporary Vault** | **[OWNER] approve R3–R8 for `$ID`** | `VAULT_REHEARSAL_APPROVED=$ID $R vault` | certificates issued (SANs incl. `vault.ewallet-vault.svc.cluster.local`), pinned image, 2 PVCs Bound, no PDB, ClusterIP only, `initialized: false`, HTTPS verifies with the CA | certificate, scheduling or disk error: record it, stop and report (no data yet); R9 only after approval |
| **R4 Initialise and bootstrap** | included | `… $R init`, then `… $R configure` | `pod_ready True` **while** `sealed true` recorded; unsealed; official and rehearsal bootstrap applied; `vault-describe.json` matches; operator token; root retired (403); operator cannot administer (403); untrusted CA refused | `init` reports `vault_initialised_keys_not_stored` or `vault_init_outcome_unknown`: **stop, never init again**; synthetic: stop and report; R9 deletes it after approval. A partial `configure`: rerun the phase |
| **R5 ESO and sync** | included | `… $R eso`, then `… $R consumer` | 3 ESO deployments; 25 CRDs; no cluster-scoped stores; SecretStore `Valid`; ExternalSecret `SecretSynced`; target owned by the ExternalSecret, key `connection_string`, hash equal to Vault v1 | sync failure: the cause class is in the result; stop and report |
| **R6 Consumer E2E** | included | (part of `consumer`) | redis-cli `PING` and `SET`/`GET` round trip succeed with the ESO Secret | `AUTH_FAILED` (password rejected) or `UNREACHABLE` (no connection after 30 s): stop and report |
| **R7 Negative tests** | included | `… $R negative` | 6 rejections with the expected cause and no target Secret; a non-ESO pod cannot reach Vault (control reaches Redis); Vault Services ClusterIP only; test objects removed | an allowed access or a wrong cause: **stop**, keep objects for diagnosis, report; R9 only after approval |
| **R8 Lifecycle and recovery** | included | `… $R lifecycle` | v2 propagates (consumer `AUTH_FAILED`, by design), v3 restores (`OK`); restart → sealed while Pod Ready → ESO `vault-sealed`, Secret unchanged, consumer `OK` → unseal → new reconciliation; production snapshot unchanged | any failure, **including production drift** (the phase exits non-zero and writes `snapshot-differs-*.json`): stop and report; R9 only after approval |
| **R9 Cleanup** | **[OWNER] approve deleting the rehearsal's synthetic disks and ESO's CRDs for `$ID`** | `VAULT_REHEARSAL_CLEANUP_APPROVED=$ID $R cleanup`, then `$R verify-clean` before any deletion: ESO objects only in `vault-rehearsal`, ESO cluster objects as recorded, and the live PVC → PV → disk chain equal to `volumes.json` with reclaim `Delete` and both disks confirmed present. Then only the two recorded PVCs are deleted. Namespaces, releases, PVCs, PVs and **disks** gone: a disk counts as deleted only on a 404 naming that exact disk (`describe`); any other gcloud error is *unknown* and fails. `verify-clean`: no collisions, `gke_preflight` READY, no local material or port-forward, production snapshot equal to R2 | a namespace without the label, or any identity or scope mismatch: **stop before deleting** (not ours). A step fails or a disk state is unknown: report, then rerun `cleanup` (idempotent) |
| **R10 Guarded sleep** | **[OWNER] approve the guarded sleep, KEDA pause and each `--also-approve` target for `$ID`** | acceptance §6 commands with `$CID=$ID`; then `cloud_lab.py verify --expect sleep`, `kubectl get nodes`, `kubectl get svc -A \| grep LoadBalancer`, `gcloud compute disks list --filter="name~pvc-"` | 0 nodes; no new LoadBalancer; no rehearsal disk; cost recorded | `SLEEP_BLOCKED`: report; never add flags or targets without approval |

**The rehearsal is not successful because its requests passed.** It passes only when R9 and
R10 also pass: nothing of it remains, the platform is unchanged, and the cluster is asleep.

Early stop at any point: stop the phases, keep the diagnostic evidence, report the resource state
and ask for the R9 approval (cleanup is idempotent and label-checked), then for R10. Nothing is
deleted automatically, but the cluster is not left awake longer than the owner's decision takes.

## 5. Init and recovery material

`init` creates the key file (exclusive, 0600, outside the repository) and keeps it open
**before** Vault initialises, writes the shares to it and forces them to disk before reporting
success. If the answer is lost after Vault initialised, or the write fails, it reports
`vault_initialised_keys_not_stored` instead of a generic error. No client can recover that
state: the Raft data is initialised without usable shares. In the rehearsal the data is
synthetic and R9 deletes it; in the official campaign the owner stops (RB *Recovery*).

## 6. Cost and duration

- Awake time ≈ 1.5–2 h: wake 20–30 min, R3–R8 ≈ 10 min on kind (GKE slower: disk attach,
  image pulls), cleanup ≈ 5 min, guarded sleep 15–30 min.
- 5 × e2-standard-4 in asia-southeast1 ≈ US$0.8/h on demand, so ≈ US$1.5–2.5, plus the two
  synthetic disks for the hours they exist (cents). No LoadBalancer, IP, DNS, KMS or IAM change.

## 7. Report after the rehearsal

`docs/evidence/edai2/vault/gke_rehearsal_<ID>/`: `results.jsonl`, `vault-describe.json`,
`eso-cluster-objects.json`, `volumes.json`, `baseline.json`, the preflight gates, and a README
(environment, commit, approvals, phase results, issues and fixes, cleanup and sleep proof,
cost). No screenshots: the owner's evidence comes from Campaign B. Any correction found goes into
the runbook and acceptance plan before Campaign B.

## 8. Local dry run

The complete procedure ran on kind before any GKE use:
[`rehearsal_kind_20261124T172656Z`](../../evidence/edai2/vault/rehearsal_kind_20261124T172656Z/README.md),
10 phases, 45 steps, no failure, nothing left. It is not GKE evidence.
