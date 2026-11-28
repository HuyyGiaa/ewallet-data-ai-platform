# Vault + ESO GKE technical rehearsal (Campaign A; prepared, not executed)

A temporary, synthetic deployment of the Vault + External Secrets Operator implementation
([VAULT.md](../../edai2/platform/VAULT.md)) on `ewallet-dev`, run by Claude after the owner
approves it. Its job is to find GKE-specific problems before the owner's official acceptance
([VAULT_GKE_ACCEPTANCE_CAMPAIGN.md](VAULT_GKE_ACCEPTANCE_CAMPAIGN.md), Campaign B). It ends with
everything removed and the cluster asleep.

**Status:**

- **Campaign A (V1):** executed on GKE on 2026-11-24 as `vault-rehearsal-20261124T173156Z`: `VAULT_GKE_REHEARSAL_COMPLETE`.
- **Campaign A+ (V1, §9):** KIND PASS.
- **Vault V2:** the driver now deploys the V2 design (§10). It passes on kind with simulated auto-unseal;
  on GKE (Campaign A2) it waits for P4, the Cloud KMS resources.
([report](../../evidence/edai2/vault/gke_rehearsal_vault-rehearsal-20261124T173156Z/README.md)). Nothing on this page runs without the owner's
explicit approval, and the approval names one rehearsal ID.

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

The driver is [`gke_rehearsal.sh`](../../../scripts/vault/gke_rehearsal.sh), for kind and GKE alike. V1's separate
kind-only validator `eso_local_validate.sh` was removed in V2 (it remains in Git history).

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
MANIFEST=~/ewallet-evidence/vault/vault-rehearsal-20261124T173156Z/recovery-manifest   # newest (recorded 2026-11-24T18:04:36Z)
```

That manifest is the newest on the laptop, checked offline on 2026-11-28
([P5 offline package](../../evidence/edai2/vault/p5_offline_optimization_20261128/README.md)):
the right cluster identity, 5 VMs, 83 workloads, no Vault or ESO. Before any wake it must be checked
again against the live cluster, read-only, once Billing is restored:
`python3 ops/gcp/cloud_lab.py diff-manifest --manifest $MANIFEST`. The only differences expected are
`ewallet-agents/fraud-coordinator`, `ewallet-mcp/drift-mcp` and `ewallet-mcp/feature-rag-mcp` at 0 (the
2026-11-24 guarded sleep), with nothing `missing`.

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

## 9. Campaign A+: the Feature API release, storage and wake

**Status: KIND PASS ([`rehearsal_aplus_kind_20261124T191350Z`](../../evidence/edai2/vault/rehearsal_aplus_kind_20261124T191350Z/README.md),
67 steps; fail-fast and interrupted-recovery cleanup KIND PASS in
[`rehearsal_aplus_faults_kind_20261125`](../../evidence/edai2/vault/rehearsal_aplus_faults_kind_20261125/README.md)); not run on GKE.** A GKE run needs the owner's approval for one new rehearsal ID.

### Why

Campaign A proved Vault, ESO and a redis-cli consumer on GKE. Campaign B's riskiest steps were still
unrehearsed on GKE: the real Feature API reading an ESO-managed Secret, the exact Helm commands of
G8 and G11 (`helm upgrade --reuse-values -f values-vault-eso.yaml`, `helm rollback`), the PV `Retain`
patch, and Vault data surviving a guarded sleep and wake. A+ runs them with synthetic values only.

### What is added (and what is not)

| Added | How |
|---|---|
| Real Feature API | the serving chart `infra/helm/ewallet-serving`, unchanged, installed as Helm release `rehearsal-serving` in `vault-rehearsal` with [`serving-values.yaml`](../../../scripts/vault/rehearsal/serving-values.yaml) and the production image (by digest). The post-renderer [`serving_post_render.py`](../../../scripts/vault/rehearsal/serving_post_render.py) drops the chart's NetworkPolicy in `feature-store` (owned by the production release), mounts the synthetic Feast registry, labels every object, and refuses any object outside `vault-rehearsal` |
| Synthetic Feast data | a seed Job on the same image applies and materializes the fixture of `tests/integration/test_feast_online_operator.py` into the rehearsal Redis; the registry (≈9 KB, checked to hold no credential) becomes ConfigMap `feast-registry`. No seed image, no Artifact Registry push |
| Official overlay | the ESO target in `vault-rehearsal` is named `feature-api-redis-vault`, so `values-vault-eso.yaml` applies unchanged. The "existing" Secret is `rehearsal-feast-online-redis` |
| Storage | `Retain` on both Vault PVs, a deliberate PVC deletion and the runbook's *Recovery of a Released PV*; on GKE also a guarded sleep and wake with Vault installed |
| Not added | no chart change, no new image, no extra disk (the registry is a ConfigMap, Redis is in memory), nothing in `ml-platform` or `feature-store` |

### Phases (after Campaign A's `consumer`)

Each needs `FEATURE_IMAGE=<Feature API image@sha256:…>` and, on GKE, `VAULT_REHEARSAL_APPROVED=$ID`.
Every lookup is classified as in the acceptance plan §4; only `BASELINE_OK` (HTTP 200, all fixture
features, readiness 200) passes. A 424, 5xx, timeout or no response stops the run.

| Phase | Official gate rehearsed | PASS when |
|---|---|---|
| `feature-api` | G1 (and G7's shadow check) | `SHADOW_MATCH` between `rehearsal-feast-online-redis` and `feature-api-redis-vault`; seed Job succeeds; release revision 1 reads `rehearsal-feast-online-redis`; lookup `BASELINE_OK` |
| `switch` | G8 + G9 | the upgrade (revision 2) reads `feature-api-redis-vault`, no `EWALLET_VAULT_*`; existing Secret unchanged; lookup `BASELINE_OK` |
| `negative` | G10 classes | Campaign A's six denials (the committed G10 template is proven on kind in `ml-platform`) |
| `rollback` | G11 + *Final state after G11* | rollback → old Secret, lookup OK; upgrade again → ESO Secret, lookup OK; store `Valid`, `SecretSynced`, existing Secret unchanged, no test objects, production unchanged |
| `lifecycle` | O2 | Campaign A's checks plus: the Feature API keeps serving while Vault is sealed |
| `retain` | G2's `Retain`, RB *Recovery* | both PVs `Retain`; PVCs deleted → PVs `Released`, disks present; claimRef cleared, PVCs recreated with `volumeName` → Vault initialised and sealed, unsealed, KV version and operator token intact, ESO re-syncs, lookup OK |
| GKE only: sleep and wake | G12 and the next G1 | the cloud-lab commands below |
| `after-wake` | RB 12 | Vault initialised (data survived), unsealed, KV version equal, PVs `Bound/Retain`, ESO new reconciliation, Redis seeded again, lookup OK, production unchanged |
| `cleanup`, `verify-clean` | — | as R9, plus: cleanup works from the state of each recorded volume (table below) and returns the PVs that still exist to `Delete` first, so the provisioner deletes their disks as in Campaign A; no `gcloud compute disks delete`, no PV deletion |

### GKE run (pending owner approval)

Fail-fast, enforced in two places:

- **The script.** Once a mutating phase has failed or was interrupted (a `phase-start` with no
  `phase-done`), or cleanup has started, every normal phase refuses. Only `inspect`, `cleanup` and
  `verify-clean` still run. A failed read-only `preflight` does not stop the run: its rerun after a
  wake is expected.
- **The blocks below.** Each one is a single paste in a subshell with `set -euo pipefail`. It stops
  at its first failure and ends with `BLOCK n PASS`. The next block starts with a `gate`
  (read-only), which exits 0 only when the previous step passed.

A+ passes on GKE only when blocks 1–6 all print their PASS line.

```bash
# 0. Session (READ-ONLY): variables and the guarded-sleep helper used by blocks 3 and 6 and by F2
ID=vault-rehearsal-$(date -u +%Y%m%dT%H%M%SZ)
export KUBECONFIG=~/.cache/ewallet-cloud-lab/kubeconfig CTX=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export REHEARSAL_CONTEXT=$CTX REHEARSAL_ID=$ID REHEARSAL_DIR=~/ewallet-evidence/vault/$ID CHARTS=~/ewallet-evidence/vault/charts
export R="bash scripts/vault/gke_rehearsal.sh" SLEEPM=docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z
export MANIFEST=~/ewallet-evidence/vault/vault-rehearsal-20261124T173156Z/recovery-manifest   # the newest recovery-manifest
export ALSO='["ewallet-mcp/feature-rag-mcp"]'      # exactly the --also-approve targets the owner approved for $ID
guarded_sleep() (  # <new manifest dir>: record the manifest, refuse on any unapproved target, sleep, verify
  set -euo pipefail
  python3 ops/gcp/cloud_lab.py record-manifest --campaign "$ID" --out "$1"
  python3 ops/gcp/cloud_lab.py plan-sleep --manifest "$SLEEPM" > "$1-plan-sleep.json"
  jq -e --argjson ok "$ALSO" '.verdict != "UNSAFE_TO_SLEEP" and ((.guarded_preparation.not_covered // []) - $ok) == []' \
    "$1-plan-sleep.json" >/dev/null || { echo "SLEEP NOT STARTED: see $1-plan-sleep.json (unsafe, or a target not approved)"; exit 1; }
  CLOUD_LAB_APPROVED=yes CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:$ID CLOUD_LAB_KEDA_PAUSE_APPROVAL=ewallet-dev:$ID \
    python3 ops/gcp/cloud_lab.py sleep --scale-pdb-guarded --pause-keda --manifest "$SLEEPM" --campaign "$ID" \
    $(jq -r '.[] | "--also-approve " + .' <<<"$ALSO") --execute --confirm-cluster ewallet-dev
  python3 ops/gcp/cloud_lab.py verify --expect sleep --manifest "$1" | tee "$1-verify-sleep.json"
)

# 1. [OWNER: first wake for $ID] then READ-ONLY checks; preflight is retried while cert-manager starts (≈3 min)
( set -euo pipefail
  CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest "$MANIFEST" --nodes 5 --confirm-cluster ewallet-dev --execute
  python3 ops/gcp/cloud_lab.py verify --expect awake --manifest "$MANIFEST"
  for i in $(seq 10); do if $R preflight; then break; fi; [ "$i" -lt 10 ] || exit 1; sleep 60; done
  echo "BLOCK 1 PASS" )
export FEATURE_IMAGE=$(helm --kube-context $CTX get values ewallet-serving -n ml-platform -o json | jq -r .featureApi.image)   # READ-ONLY

# 2. [OWNER: R3–R8 and the A+ phases for $ID] the first failure ends the block; the script refuses the rest
( set -euo pipefail
  [[ $FEATURE_IMAGE =~ @sha256:[0-9a-f]{64}$ ]] || { echo "FEATURE_IMAGE is not pinned by digest: nothing started"; exit 1; }
  for p in vault init configure ceremony eso consumer feature-api switch negative rollback lifecycle retain; do   # V2; seal and agents are kind-only
    VAULT_REHEARSAL_APPROVED=$ID $R "$p"
  done
  $R gate retain
  echo "BLOCK 2 PASS" )

# 3. [OWNER: guarded sleep, KEDA pause and the $ALSO targets for $ID] the first sleep, WITH Vault installed
( set -euo pipefail; $R gate retain; guarded_sleep "$REHEARSAL_DIR/recovery-manifest-mid"; echo "BLOCK 3 PASS" )

# 4. [OWNER: second wake for $ID] the same Vault comes back on the retained disks
( set -euo pipefail
  $R gate retain
  CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest "$REHEARSAL_DIR/recovery-manifest-mid" --nodes 5 --confirm-cluster ewallet-dev --execute
  python3 ops/gcp/cloud_lab.py verify --expect awake --manifest "$REHEARSAL_DIR/recovery-manifest-mid"
  VAULT_REHEARSAL_APPROVED=$ID $R after-wake
  $R gate after-wake
  echo "BLOCK 4 PASS: every A+ phase passed" )

# 5. [OWNER: cleanup for $ID] then READ-ONLY verification
( set -euo pipefail; VAULT_REHEARSAL_CLEANUP_APPROVED=$ID $R cleanup; $R verify-clean; $R gate clean; echo "BLOCK 5 PASS" )

# 6. [OWNER: second guarded sleep for $ID] a FRESH manifest without the rehearsal workloads; Campaign B's G1 wakes from it
( set -euo pipefail; $R gate clean; guarded_sleep "$REHEARSAL_DIR/recovery-manifest"; echo "BLOCK 6 PASS: asleep" )
```

- **Approvals for one ID:**
  - the first wake (block 1);
  - R3–R8 and the A+ phases (block 2);
  - the first guarded sleep, with its KEDA pause and each target in `$ALSO` (block 3). So far that is
    `ewallet-mcp/feature-rag-mcp`; Campaign A's approval does not carry over. `guarded_sleep` refuses
    any `not_covered` target outside `$ALSO`, so a new target needs a new approval;
  - the second wake (block 4);
  - cleanup (block 5): it deletes the two synthetic disks and ESO's CRDs;
  - the second guarded sleep (block 6).
- **Keep `$REHEARSAL_DIR/private` across the sleep:** it holds the synthetic operator token and recovery
  keys that `after-wake` needs (V2 needs no shares to unseal: Vault unseals itself through Cloud KMS). Cleanup removes it.
- **V2 prerequisite:** on GKE, `preflight` (block 1) also runs `scripts/vault/kms_preflight.py --mode cloud`. The `vault`
  phase proceeds only after this run recorded `kms-preflight` PASS, which requires P4-B to have created the Cloud KMS resources
  ([runbook step 0](VAULT_ESO_RUNBOOK.md#0-cloud-kms-p4-b-separate-owner-approval-per-step-not-done)). Until then block 1 stops at the preflight.
- **The mid-campaign manifest includes the rehearsal workloads** (Vault, ESO, `rehearsal-serving`,
  Redis), so the second wake restores them. The final manifest (block 6) is recorded after cleanup;
  Campaign B's G1 uses that one.

### Failure: a block that does not print its PASS line

1. **Stop.** Do not paste the next block. Its gate refuses anyway, and the script refuses every
   normal phase once one has failed or was interrupted. Never re-run a failed mutating phase. Never
   delete a namespace, PV or disk by hand.
2. **Read-only report:** run `$R inspect; tail -n 5 "$REHEARSAL_DIR/results.jsonl"; python3 ops/gcp/cloud_lab.py status`.
   `inspect` records:
   - why the run stopped;
   - the three namespaces (absent, this rehearsal's, or not);
   - the rehearsal's Helm releases;
   - the state of each recorded Vault volume, with any problem;
   - on GKE, each recorded disk (present, absent or unknown);
   - the decision left to the owner.
3. **The owner decides one of the following before anything else runs.** The cluster is never left
   awake without one of them.

| | When | Commands |
|---|---|---|
| **F1** cleanup, then the final sleep | Vault was installed, and `inspect` shows no volume problem | [OWNER: cleanup for `$ID`] block 5, then [OWNER: second guarded sleep] block 6 |
| **F2** guarded sleep as it is | the failure came before `vault` (nothing installed); cleanup refused; or the owner decides later | [OWNER: guarded sleep for `$ID`] `( set -euo pipefail; guarded_sleep "$REHEARSAL_DIR/recovery-manifest-stopped" )`. Later: wake from that manifest, then F1. The `Retain` PVs keep the disks; a few cents a day |
| sleep blocked | `guarded_sleep` printed `SLEEP NOT STARTED`, or `cloud_lab.py sleep` printed `SLEEP_BLOCKED` | report the plan or the refusal; never add a flag or a target without approval. Then re-run the same block with the owner's new approval, or F1 |

**Cleanup is resumable.** It starts from the actual state of each recorded volume:

- after a transient error (an API timeout), the same command can run again once the owner has seen
  the reason;
- when it refuses with "nothing deleted, owner decision", it is not retried: F2, and the cause is
  investigated.

| Volume state (`volume_states.jq`) | Left by | What cleanup does |
|---|---|---|
| `bound` | before the PVC deletion, or after the recovery | PV back to `Delete`, then the PVC is deleted with the release; the provisioner deletes the disk |
| `released` | a PVC deleted, recovery not reached | PV back to `Delete`: the provisioner deletes the PV and its disk |
| `unclaimed` (PV `Available`, claimRef cleared) | stopped between the claimRef removal and the new PVC | finishes the runbook recovery first (PVC with `volumeName`, needs `ewallet-vault` labelled with this ID), then as `bound`. An `Available` PV is never reclaimed, and the script never deletes a PV object |
| `binding` (PVC recreated, not yet Bound) | stopped while binding | waits for the bind, then as `bound`; refuses if it does not bind |
| `gone` | a cleanup retried after the volume was deleted | nothing; its disk must still be proven absent |
| anything else: a disk or claim that differs from the record, an unrecorded PV or PVC in `ewallet-vault`, a `Failed` or `Lost` volume, a missing `volumes.json` while volumes exist, a disk that cannot be read | — | refuses before the first deletion |

Every cleanup ends with each recorded disk proven absent: an HTTP 404 that names that exact disk.
A disk that is still present after its PV is gone (for example, a `Retain` PV deleted by hand)
fails cleanup. The script never deletes a disk itself; the owner decides.

**Fault-injection tests (kind only).** `REHEARSAL_FAULT=<point>` stops `retain` (or `cleanup`) at
a named point, and the script refuses it on GKE. The points are:

- `retain-set`;
- `pvc-deleted:audit-vault-0`;
- `pvcs-released`;
- `claimref-removed:audit-vault-0`;
- `rebound:audit-vault-0`;
- `rebound:data-vault-0`;
- `cleanup-reclaim-restored`.

`tests/ops/test_vault_rehearsal.py` covers every state in the table against the real script.

### Two-node run (`vault-a2` partial wake): the P5 recommendation

Campaign A2 runs on 2 nodes, through the `experiment` profile `vault-a2`
([P5 offline package](../../evidence/edai2/vault/p5_offline_optimization_20261128/README.md)). Restoring
the whole platform is a separate wake on 5 nodes (blocks R1 and R2).

**Rules of this run:**

1. **Status: prepared offline, not executed.** No real GKE validation of this run has occurred, and no
   partial wake has ever run on this cluster. Offline evidence and the kind runs are not GKE evidence.
2. **Both Campaign A2 wakes use 2 nodes:** the first (T1) and the second (T4).
3. **Restoring the full platform is a separate 5-node procedure, R1/R2.** It is not part of A2's 2-node run.
4. **R1 may be deferred.** The platform restoration then stays explicitly **pending**: T6's PASS line says
   so, the campaign report records it, and the next wake of the cluster must be R1.
5. **Every mutating operation needs the owner's explicit approval.** That means every wake, sleep, cleanup,
   restoration and recovery step.
6. **The fall-back to 5 nodes (T-FB) is never automatic.** It needs its own explicit owner approval. A
   failed `verify_partial` only stops the block.

- **The profile** allow-lists only the rehearsal's own namespaces and KEDA's metrics apiserver. Every
  other production workload is held at 0, and the system namespaces run as usual.
- **Why A2 needs no production pod:** `compare_production` reads Helm releases, Secrets, Deployment
  templates and Services, never replicas.

| Step | Nodes | What it does |
|---|---|---|
| T1 | 0 → **2** | read-only pre-wake checks, the partial wake, `verify_partial`, preflight |
| block 2 | 2 | the rehearsal phases, unchanged |
| T3 | 2 → **0** | records the rehearsal workloads, then a plain sleep with Vault installed |
| T4 | 0 → **2** | the second wake: the rehearsal workloads back as recorded, then `after-wake` (Vault unsealed by Cloud KMS on its retained disks) |
| block 5 | 2 | cleanup, unchanged |
| T6 | 2 → **0** | a plain sleep. Production differs from `$MANIFEST` only by held-down zeros |
| R1 | 0 → **5** | full restoration: `wake-full` from `$MANIFEST`, then `verify --expect awake` |
| R2 | 5 → **0** | `guarded_sleep`, which records the fresh recovery manifest the next campaign wakes from |

R1 can be the opening wake of the next campaign that needs the platform (Campaign B's G1). R2 is then
that campaign's own closing sleep.

**Owner approval: every step that changes the cluster needs one.** That covers:

- T1, T3, T4, T6, R1 and R2;
- blocks 2 and 5;
- each recovery row below.

The T and R blocks start with `approved <step>`. It refuses unless the owner has exported
`A2_APPROVAL=<step>:$ID` for that step. Blocks 2 and 5 keep their own approvals:
`VAULT_REHEARSAL_APPROVED` and `VAULT_REHEARSAL_CLEANUP_APPROVED`.

"Plain sleep" means only this: when `plan-sleep` reports `SAFE_TO_SLEEP`, no **additional** PDB-guarded,
KEDA-pause or `--also-approve` approval is needed. The sleep itself is still approved by the owner. If
`plan-sleep` reports anything else, the helper stops; never add a flag to get past it. R2 is a guarded
sleep and needs the guarded, KEDA and `$ALSO` approvals, as block 6 does.

**Why no recovery manifest is recorded before T3 and T6:**

- `record-manifest` records the live declared replicas. That is right before a full sleep, as in
  block 3, because the live cluster *is* the platform then.
- During a partial wake, production is held at 0. A manifest recorded then would make the next full
  wake restore those zeros. `record-manifest` refuses while a cloud-lab pause exists, and this flow
  never calls it.

**What replaces the recording, so `$MANIFEST` is not trusted blindly:**

- **T1:** before anything changes, the live declared state is compared with `$MANIFEST` (only the three
  guarded Deployments may differ, and nothing may be missing or unrecorded) and saved.
- **The rehearsal's own workloads** (Vault, ESO, `rehearsal-serving`, Redis) were never in `$MANIFEST`.
  T3 records them before the sleep, and T4 requires the same set, at the same replicas and Ready, before
  `after-wake`.
- **`after-wake`** checks the rest: Vault initialised and auto-unsealed with `gcpckms`, the KV version,
  both PVs `Bound/Retain`, a new ESO reconciliation, the consumer, the Feature API lookup, and production
  unchanged.
- **R1:** the full restoration is proven against `$MANIFEST`. R2 then records the fresh manifest from the
  fully awake platform, as block 6 does.

**What stays different during A2:**

- The two WorkerPool Deployments (`ewallet-sandbox/business-agents-deployment` and
  `kagent-pilot/kagent-pilot-deployment`) cannot be held down. They come back without their controller,
  and they are counted in the 2 nodes.
- The ScaledObjects that the 2026-11-24 sleep paused stay paused until R1.

```bash
# T0. Session (READ-ONLY), after block 0: the approval check, the profile and the helpers of the T and R blocks
export P="--profile vault-a2 --manifest $MANIFEST"; mkdir -p "$REHEARSAL_DIR"
export SYS='["kube-system", "gmp-system", "gke-managed-cim", "istio-system", "cert-manager"]'   # cloud_lab SYSTEM_NAMESPACES: their autoscalers follow the node count
approved() {  # <step>: the owner's explicit approval of this step for this ID, exported before pasting the block
  [ "${A2_APPROVAL:-}" = "$1:$ID" ] || { echo "NOT APPROVED: step $1 needs the owner's A2_APPROVAL=$1:$ID"; exit 1; }
}
verify_partial() (  # the partial wake: rehearsal allowed, production held at 0, APIServices Available, no fail-closed webhook down
  set -euo pipefail
  for i in $(seq 10); do if python3 ops/gcp/cloud_lab.py verify --expect awake --mode experiment $P > "$REHEARSAL_DIR/verify-partial.json"; then break; fi
    [ "$i" -lt 10 ] || { cat "$REHEARSAL_DIR/verify-partial.json"; exit 1; }; sleep 60; done
)
rehearsal_workloads() {  # the Deployments and StatefulSets of the rehearsal namespaces: kind, namespace, name, replicas
  for ns in ewallet-vault external-secrets vault-rehearsal; do kubectl --context "$CTX" -n "$ns" get deploy,sts -o json; done \
    | jq -cs '[.[].items[] | {kind, namespace: .metadata.namespace, name: .metadata.name, replicas: .spec.replicas}] | sort_by(.namespace, .kind, .name)'
}
plain_sleep() (  # <label>: only when SAFE_TO_SLEEP; a plain sleep; then the production disks and buckets of $MANIFEST
  set -euo pipefail
  python3 ops/gcp/cloud_lab.py plan-sleep > "$REHEARSAL_DIR/$1-plan-sleep.json"
  jq -e '.verdict == "SAFE_TO_SLEEP"' "$REHEARSAL_DIR/$1-plan-sleep.json" >/dev/null \
    || { echo "SLEEP NOT STARTED: see $REHEARSAL_DIR/$1-plan-sleep.json (not SAFE_TO_SLEEP: owner decision, no guarded flag)"; exit 1; }
  CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py sleep --execute --confirm-cluster ewallet-dev
  python3 ops/gcp/cloud_lab.py verify --expect sleep --manifest "$MANIFEST" | tee "$REHEARSAL_DIR/$1-verify-sleep.json"
)

# T1. [OWNER: approve T1 (A2_APPROVAL=T1:$ID): first wake, 2 nodes, profile vault-a2] after READ-ONLY checks of the live state
( set -euo pipefail; approved T1
  python3 ops/gcp/cloud_lab.py diff-manifest --manifest "$MANIFEST" > "$REHEARSAL_DIR/diff-before-wake.json"
  jq -e --argjson sys "$SYS" 'def app: split("/")[0] as $n | $sys | index($n) | not;
         ([.differs[].workload | select(app)] - ["ewallet-agents/fraud-coordinator", "ewallet-mcp/drift-mcp", "ewallet-mcp/feature-rag-mcp"]) == []
         and .missing == [] and ([.unrecorded[] | select(app)] == [])' \
    "$REHEARSAL_DIR/diff-before-wake.json" >/dev/null || { echo "the cluster differs from \$MANIFEST: nothing woken"; exit 1; }
  kubectl --context "$CTX" get deploy,sts -A -o json \
    | jq '[.items[] | {kind, namespace: .metadata.namespace, name: .metadata.name, replicas: .spec.replicas}]' > "$REHEARSAL_DIR/live-before-wake.json"
  # The partial wake scales production StatefulSets to 0: none may delete its PVCs when scaled (the default is Retain)
  kubectl --context "$CTX" get sts -A -o json | jq -e '[.items[] | select(.spec.persistentVolumeClaimRetentionPolicy.whenScaled == "Delete")
    | "\(.metadata.namespace)/\(.metadata.name)"] == []' || { echo "a StatefulSet deletes its PVCs when scaled: nothing woken"; exit 1; }
  CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-experiment $P --nodes 2 --confirm-cluster ewallet-dev --execute
  verify_partial
  for i in $(seq 10); do if $R preflight; then break; fi; [ "$i" -lt 10 ] || exit 1; sleep 60; done
  echo "BLOCK T1 PASS" )

# then block 2 above, unchanged (its own approval: VAULT_REHEARSAL_APPROVED=$ID)

# T3. [OWNER: approve T3 (A2_APPROVAL=T3:$ID): plain sleep] the first sleep, WITH Vault installed; the two Vault disks must stay
( set -euo pipefail; $R gate retain; approved T3
  rehearsal_workloads > "$REHEARSAL_DIR/rehearsal-workloads-mid.json"
  jq -e 'any(.[]; .kind == "StatefulSet" and .name == "vault") and any(.[]; .name == "feature-api") and any(.[]; .name == "external-secrets")' \
    "$REHEARSAL_DIR/rehearsal-workloads-mid.json" >/dev/null || { echo "the rehearsal workloads are incomplete: not slept"; exit 1; }
  plain_sleep mid
  $R inspect; tail -n 1 "$REHEARSAL_DIR/results.jsonl" | jq -e '[.disks[]] | length == 2 and all(. == "present")' >/dev/null
  echo "BLOCK T3 PASS" )

# T4. [OWNER: approve T4 (A2_APPROVAL=T4:$ID): second wake, 2 nodes] the same Vault comes back on the retained disks and unseals through Cloud KMS
( set -euo pipefail
  $R gate retain; approved T4
  CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-experiment $P --nodes 2 --confirm-cluster ewallet-dev --execute
  verify_partial
  [ "$(rehearsal_workloads)" = "$(jq -c . "$REHEARSAL_DIR/rehearsal-workloads-mid.json")" ] \
    || { echo "the rehearsal workloads differ from rehearsal-workloads-mid.json"; exit 1; }
  jq -r '.[] | "\(.namespace) \(.kind | ascii_downcase)/\(.name)"' "$REHEARSAL_DIR/rehearsal-workloads-mid.json" | while read -r ns w; do
    kubectl --context "$CTX" -n "$ns" rollout status "$w" --timeout=600s >/dev/null || { echo "$ns $w not Ready after the wake"; exit 1; }
  done || exit 1   # explicit: errexit does not reliably stop at a failed "| while" pipeline
  VAULT_REHEARSAL_APPROVED=$ID $R after-wake
  $R gate after-wake
  echo "BLOCK T4 PASS: every A+ phase passed" )

# then block 5 above, unchanged (its own approval: VAULT_REHEARSAL_CLEANUP_APPROVED=$ID)

# T6. [OWNER: approve T6 (A2_APPROVAL=T6:$ID): plain sleep] asleep; production differs from $MANIFEST only by held-down zeros
( set -euo pipefail; $R gate clean; approved T6
  plain_sleep final
  python3 ops/gcp/cloud_lab.py diff-manifest --manifest "$MANIFEST" > "$REHEARSAL_DIR/diff-final.json"
  jq -e --argjson sys "$SYS" 'def app: split("/")[0] as $n | $sys | index($n) | not;
         all(.differs[] | select(.workload | app); .live == 0) and .missing == [] and ([.unrecorded[] | select(app)] == [])' \
    "$REHEARSAL_DIR/diff-final.json" >/dev/null
  echo "BLOCK T6 PASS: A2 done on 2 nodes; asleep; PLATFORM RESTORATION PENDING until R1 passes" )

# R1. [OWNER: approve R1 (A2_APPROVAL=R1:$ID): full wake, 5 nodes] the whole platform back to $MANIFEST; the ScaledObjects unpaused
( set -euo pipefail; $R gate clean; approved R1
  CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest "$MANIFEST" --nodes 5 --confirm-cluster ewallet-dev --execute
  python3 ops/gcp/cloud_lab.py verify --expect awake --manifest "$MANIFEST" | tee "$REHEARSAL_DIR/verify-restored.json"
  echo "BLOCK R1 PASS: platform restored (the next campaign may continue from here)" )

# R2. [OWNER: approve R2 (A2_APPROVAL=R2:$ID) AND the guarded sleep, KEDA pause and $ALSO targets] a FRESH manifest of the full platform
( set -euo pipefail; $R gate clean; approved R2
  python3 ops/gcp/cloud_lab.py verify --expect awake --manifest "$MANIFEST" >/dev/null
  guarded_sleep "$REHEARSAL_DIR/recovery-manifest"
  echo "BLOCK R2 PASS: asleep; the next wake uses \$REHEARSAL_DIR/recovery-manifest" )
```

**Failure in the two-node run.** The steps of *Failure* above apply, with `plain_sleep` in place of
`guarded_sleep`. Each row is an owner decision with its own approval:

| | When | Commands |
|---|---|---|
| **T-F0** the wake did not finish | `wake-experiment` stopped before or during the resize | read-only `cloud_lab.py status`. Production is held at 0, and that is harmless. [OWNER: approve again] re-run T1 or T4, or T-F2 if nodes came up |
| **T-F1** cleanup, then the final sleep | as F1 | [OWNER: cleanup] block 5, then [OWNER: T6] T6 |
| **T-F2** plain sleep as it is | as F2 | [OWNER: approve T-F2 (`A2_APPROVAL=T-F2:$ID`)] `( set -euo pipefail; approved T-F2; plain_sleep stopped )`. Later: T4, then T-F1 |
| **T-FB** fall back to 5 nodes (never automatic) | `verify_partial` failed in T1 (an APIService not Available, a fail-closed webhook without an endpoint, a workload not held down); nothing of Vault is installed yet | the block only stops. [OWNER: a separate, explicit approval of the 5-node run] `wake-full --manifest $MANIFEST --nodes 5`, then blocks 1 to 6 above, each with its approvals |
| **T-FR** restoration failed | R1's `verify` lists failures | stay awake, read-only report; [OWNER: decide] a repeated R1 or R2 as it is. Never `record-manifest` before `verify --expect awake` passes |

### Resources and cost

Additional to Campaign A: Feature API 100m CPU / 384 MiB (one pod), a seed Job, a ConfigMap; no
disk. Awake time ≈ 70–90 min in two stretches (≈ 35 min for wake, R2–R8 and A+; ≈ 25 min for the
second wake, `after-wake`, cleanup), plus three cloud-lab transitions. At the `cloud_lab.py` rate of
≈ US$0.83/h for 5 nodes plus ≈ US$0.10/h cluster fee: ≈ US$1.2–1.5. The two synthetic disks
(6 GiB) cost cents while asleep. The two-node run (estimates, every wake included; the
[P5 package](../../evidence/edai2/vault/p5_offline_optimization_20261128/README.md) has the comparison):

- T1–T6 on 2 nodes: ≈ US$0.4–0.7.
- With R1/R2 on 5 nodes straight after: ≈ US$1.0–1.7, against ≈ US$1.7–2.7 for blocks 1–6 with the same
  step durations.

### What A+ still cannot show

The production registry in GCS through Workload Identity; `feature-store/redis` and its
`redis-clients-feature-api` NetworkPolicy; the Feature API's KEDA ScaledObject; the real credential
and `SHADOW_MATCH` against `feast-online-redis`; anything in `ml-platform`. Those remain Campaign B
risks, each guarded by a stop condition in the acceptance plan.

## 10. Vault V2 on kind

**Status: KIND PASS.** Full campaign
[`rehearsal_v2_kind_vault-rehearsal-20261125T074917Z`](../../evidence/edai2/vault/rehearsal_v2_kind_vault-rehearsal-20261125T074917Z/README.md):
129 records, 0 FAIL. Fault runs: [`rehearsal_v2_faults_kind_20261125`](../../evidence/edai2/vault/rehearsal_v2_faults_kind_20261125/README.md).
Auto-unseal there is `SIMULATED_AUTO_UNSEAL`. GKE (Campaign A2) waits for P4.

`scripts/vault/gke_rehearsal.sh` deploys the Vault V2 design
([audit](../../edai2/platform/VAULT_V2_ARCHITECTURE_AUDIT.md)):

- the `gcpckms` seal on GKE;
- on kind, a local transit "unsealer" Vault as the stand-in, labelled `SIMULATED_AUTO_UNSEAL`;
- recovery keys;
- the rehearsal ClusterSecretStore `ewallet-vault-rehearsal`, with exact namespaces `vault-rehearsal`
  and `vault-rehearsal-agents`. It logs in as the official reader `external-secrets/eso-vault-reader`
  through the narrower role `eso-vault-rehearsal`.

On kind it also creates the official `ewallet-vault` from the committed file. On GKE it never does:
there the official store belongs to the campaign.

**Kind-only parts:**

- the unsealer and the `seal` drill: the seal becomes unreachable, and that is never applied to Cloud KMS;
- `ewallet-sandbox` as a test namespace;
- the `agents` phase: the real agentgateway and the real vLLM CPU image, on the staged Qwen3-0.6B model,
  through a read-only hostPath PV.

**Order (kind):**

```bash
kind_cert_manager.sh install kind-llm-agent-local   # scripts/vault/rehearsal/; refuses an existing cert-manager
preflight vault init configure ceremony seal eso consumer feature-api switch negative rollback agents lifecycle retain
gate retain
# simulated wake: delete pod vault-0 and the synthetic Redis pod
after-wake
gate after-wake
cleanup
verify-clean
gate clean
kind_cert_manager.sh remove kind-llm-agent-local    # removes only the cert-manager it installed
```

| Phase | PASS when |
|---|---|
| `vault` | the unsealer is ready (its token may only encrypt and decrypt with key `autounseal`; its root revoked); Vault 2.0.4 runs with `seal_type` `transit` (kind) or `gcpckms` (GKE); uninitialised and Ready; TLS verified |
| `init` | 3 recovery keys, threshold 2, no unseal key; Vault **unseals itself**; Ready; manual `unseal` refused (`vault_auto_unseal`) |
| `configure` | official + rehearsal bootstrap; roles exactly `eso-ewallet-reader`, `eso-vault-rehearsal`, `vault-admin`; root retired (403); operator cannot administer; untrusted CA refused |
| `ceremony` | admin login; 1 recovery key cannot finish (cancelled); a forged share with a genuine one is rejected (400); 2 keys generate a root that reads the configuration, then is retired (403) |
| `seal` (kind) | restart → auto-unsealed, Raft data and operator token intact; unsealer unreachable → never Ready, exits on seal setup; unsealer back → unsealed again by itself |
| `eso` | controller flags (cluster store on; ClusterExternalSecret, PushSecret, ClusterPushSecret off); CA published once (`CA_PUBLISHED`, then `CA_UNCHANGED`); `ewallet-vault` (kind) and `ewallet-vault-rehearsal` Ready |
| `consumer` | one store, two namespaces, three ExternalSecrets synced, each target owned by its ExternalSecret, one key, equal to Vault; Redis round trip OK |
| `feature-api`, `switch`, `rollback` | as A+ (§9), with target `feast-online-redis-vault` and the official overlay unchanged |
| `negative` | out-of-policy path, missing key, wrong ServiceAccount, same-named reader in a consumer namespace, wrong audience (store NotReady), untrusted CA (store NotReady), unapproved namespace, sandbox (kind, official store): each with its own cause and no target Secret; the reader's own token: read 200 in its prefix, 403 for an out-of-prefix read, metadata, list, write, `sys/` and root generation; NetworkPolicy blocks a non-ESO pod |
| `agents` (kind) | `SHADOW_MATCH` for both keys; through the gateway with real vLLM: valid 200, wrong, missing and backend key 401; the official overlay moves all three references; client-key rotation (old key until the restart, then 401); a half-rotated backend key fails and the real vLLM refuses it directly; Helm rollback restores 200; re-upgrade on the rotated keys |
| `lifecycle` | KV v2/v3 propagation; a real Redis rotation (server password, Vault, ESO, consumers; the old credential refused); restart → auto-unsealed, ESO resumes; (kind) seal unreachable → Secrets kept, consumers serve and start; seal back → recovered by itself |
| `retain`, `after-wake` | as A+, without any unseal: the re-bound and woken Vault unseals itself |
| `cleanup`, `verify-clean` | as A+, plus: the rehearsal namespaces, every ClusterSecretStore labelled with this id (any other refuses cleanup), the agents release and the model PV (by recorded name); the KMS key is never touched |
