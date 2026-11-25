# Vault + ESO GKE acceptance campaign (prepared, not executed)

Gate-by-gate plan for deploying the existing Vault + External Secrets Operator implementation
([VAULT.md](../../edai2/platform/VAULT.md)) on `ewallet-dev`, and for capturing the owner's evidence.
Commands come from [VAULT_ESO_RUNBOOK.md](VAULT_ESO_RUNBOOK.md) (cited as *RB step N*) and
`ops/gcp/cloud_lab.py`.

**Status: Stage A (preparation) complete; Stage B (live) not started.** Nothing here runs without
the owner's explicit approval of the campaign. Every **[OWNER]** step is performed by the owner
personally. Nothing in this page wakes GKE.

**Order:** the technical rehearsal ([VAULT_GKE_REHEARSAL.md](VAULT_GKE_REHEARSAL.md), Campaign A,
synthetic, removed afterwards) runs first. It ran on 2026-11-24 and passed
([report](../../evidence/edai2/vault/gke_rehearsal_vault-rehearsal-20261124T173156Z/README.md)); its corrections are applied here.
Campaign A+ ([§9](VAULT_GKE_REHEARSAL.md#9-campaign-a-the-feature-api-release-storage-and-wake)) adds the real Feature API on an
ESO Secret through the exact G8/G11 Helm commands, the `Retain` drill and a sleep/wake with Vault installed:
KIND PASS (with fail-fast gates and cleanup from any point of the `Retain` drill), GKE pending approval. This official campaign (Campaign B) starts after the owner has reviewed
the rehearsal reports.

## 1. Baseline (2026-11-24)

| Item | Value |
|---|---|
| Source | `feature/edai2-vault-eso`, with `main` 222932c2 merged in (2026-11-24); not on main |
| Cluster | `ewallet-dev`, `asia-southeast1-b`, GKE 1.36.4-gke.2377000, REGULAR channel, Calico NetworkPolicy enforced, not a private-node cluster |
| Node pool | `cpu-pool`, e2-standard-4, single zone b, no autoscaling, **0 nodes (asleep)** |
| Access | `KUBECONFIG=~/.cache/ewallet-cloud-lab/kubeconfig` (DNS endpoint); context `gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev` |
| Wake manifest | `~/ewallet-evidence/vault/vault-rehearsal-20261124T173156Z/recovery-manifest`: the newest under `~/ewallet-evidence`, written by `cloud_lab record-manifest` at 2026-11-24T18:04:36Z before the rehearsal's guarded sleep (83 workloads). **Always use the newest** (`$MANIFEST`, §3) |
| Read-only preflight | `--mode preinstall`, [`gke_preflight_20261124T165422Z`](../../evidence/edai2/vault/gke_preflight_20261124T165422Z/gates-preinstall.jsonl): `VAULT_GKE_PREFLIGHT_READY`, including the new gates for leftover Vault volumes (any phase, also `Released`), leftover chart cluster objects and the rehearsal namespace; `cert-manager-ready` NOT_CHECKED (asleep) |

### Preflight modes

`scripts/vault/gke_preflight.py` is read-only (get, list, version, template). It fails closed:
a failed or unreadable query is FAIL (`QUERY_FAILED` or `DATA_INVALID`), never PASS and never "absent".

| Mode | When | Checks |
|---|---|---|
| `preinstall` | G1 (and now, asleep; also the rehearsal's R0, R2 and post-cleanup check) | no earlier Vault/ESO namespace (incl. `vault-rehearsal`), CRD, release, PVC, Vault PV in any phase, or chart ClusterRole/binding/webhook; cert-manager CRDs and, when awake, its deployments; `standard-rwo`; `feast-online-redis` present and non-empty; `ewallet-serving` release, drift and overlay scope |
| `consumer-switch` | G8, before the upgrade | cluster awake; `vault` release deployed; ESO's 3 deployments available; ExternalSecret `SecretSynced`; target Secret owned by the ExternalSecret and non-empty; `SHADOW_MATCH`; Feature API available and still on `feast-online-redis`; release drift and overlay scope. Existing Vault/ESO resources are expected here and never block |

### Preflight gates (now)

| Gate | Expected | Observed | Status | Required action |
|---|---|---|---|---|
| Git baseline | Vault branch with `main` 222932c2 merged in; clean tree | as expected | PASS | — |
| Cluster reachable (live) | API answers on the pinned context | v1.36.4-gke.2377000 | PASS | — |
| No earlier Vault/ESO (live) | no namespace, ESO CRD, release, Vault PVC or PV, chart cluster object | confirmed absent (2026-11-24 16:54 UTC) | PASS | the rehearsal must leave the same state (its `verify-clean`) |
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
| IMPORTANT | `init` succeeds in Vault but the shares never reach disk (lost answer, full disk) | `init` creates and holds the key file before calling Vault and fsyncs it; such a failure is reported as `vault_initialised_keys_not_stored` or `vault_init_outcome_unknown`. Stop: never initialise again (RB *Recovery*). Vault holds no secret yet at G3, so the owner may decide to start over |
| IMPORTANT | Pod Ready does not mean unsealed (`sealedcode=204`) | Every gate and caption uses `$V status` (`sealed: false`), never the Workloads page alone |

## 3. Campaign gates (Stage B)

Campaign ID `vault-gke-<UTC timestamp>` (`$CID`). Working directory `~/ewallet-evidence/vault/$CID/`
(mode 700); recovery material only in `~/ewallet-evidence/vault/private/`. On every command:
`export KUBECONFIG=~/.cache/ewallet-cloud-lab/kubeconfig`, plus `--context $CTX` (`--kube-context` for helm).
`P="python3 scripts/vault/gke_preflight.py --kubeconfig $KUBECONFIG --context $CTX --out ~/ewallet-evidence/vault/$CID/preflight"`.
`MANIFEST=<the newest ~/ewallet-evidence/**/recovery-manifest>`, checked at G1 (after the rehearsal, the one its R10 recorded).

| Gate | Owner approval | Actions (RB = runbook step) | Pass when | Stop / rollback |
|---|---|---|---|---|
| **G1 Wake and baseline** | **[OWNER] approve the wake** | `CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest $MANIFEST --nodes 5 --confirm-cluster ewallet-dev --execute`; `cloud_lab.py verify --expect awake --manifest $MANIFEST`; `$P --mode preinstall`; record the `feast-online-redis` resourceVersion, `helm -n ml-platform history ewallet-serving`, the Feature API `secretKeyRef`, and every live release revision (`helm --kube-context $CTX list -A -a -o json > ~/ewallet-evidence/vault/$CID/helm-g1.json`; no expected numbers: they change between campaigns); baseline lookup and its G1 class (§4). `verify --expect awake` passes before cert-manager is available (≈3 min after the wake, seen in the rehearsal): rerun the preflight until `cert-manager-ready` is PASS | preinstall READY with `cert-manager-ready` PASS; G1 class `BASELINE_OK` or `BASELINE_DATA_UNAVAILABLE`; workloads healthy; baseline lookup recorded | Unhealthy platform or preflight BLOCKED/FAIL: stop, then sleep (§6) |
| **G2 Vault infrastructure** | **[OWNER] approve Stage B mutations, and the PV `Retain` patch** | RB 2: namespace, certificate (wait Ready), `helm install vault … -f infra/kubernetes/vault/values-gke.yaml` (only that file; it carries the disabled PDB), NetworkPolicy, `vault-admin` SA; PVCs Bound; **`Retain` on both PVs before G3** | pod Running; PVCs Bound; PVs `Retain`; `kubectl -n ewallet-vault get pdb` returns none; Services ClusterIP only; `$V status` over HTTPS with the published CA: `initialized: false` | Certificate or TLS error: `helm uninstall vault` (no data yet), fix, retry |
| **G3 Initialise** | **[OWNER-ONLY]** | RB 3: `$V init --keys-out $KEYDIR/vault-keys.json`; copy the file to the two offline media | `initialized: true`; key file 0600 outside the repo | Already initialised: **stop**, never initialise again |
| **G4 Auth and policies** | **[OWNER] unseal** | RB 4: `unseal`, `status`, `configure --keys-file`, **`describe --keys-file` (capture E3 now: it needs the initial root)**, `operator-token`, `retire-root`, then the root probe `configure --keys-file` | `sealed: false`; `configure` lists `audit, kv, kubernetes-auth`, the policies (`eso-ewallet-agents, eso-ml-platform, feature-api, fraud-api, root-generation, secret-operator`) and roles (`eso-*`, `feature-api`, `fraud-api`, `vault-admin`); `operator_token_created: true, orphan: true`; `root_token_revoked: true`; the root probe fails with `returned 403`. No secret is read or written in G4 | Partial `configure`: re-run it (idempotent) before retiring root |
| **G5 Seed Redis credential** | **[OWNER] approve the copy** | RB 5: pipe `feast-online-redis` → `$V put --token-file $OP --path feature-api/feast-redis --key connection_string --stdin`; `$V versions --token-file $OP --path feature-api/feast-redis`; compare the Secret's resourceVersion with G1 | `put` returns `version: 1`; `versions` (operator token) shows `current_version: 1` and a time, no value; `feast-online-redis` resourceVersion unchanged; Redis untouched | Missing or empty Secret, or `put` refused: stop |
| **G6 ESO** | included in G2 | RB 6: `vault-ca` ConfigMap (create, never apply over an existing one), ESO namespace, `helm install external-secrets … --wait`; `kubectl get clustersecretstore,clusterexternalsecret,pushsecret -A` | 3 deployments Ready; CRDs installed; none of those cluster-scoped objects | Install failed: `helm uninstall external-secrets` (nothing depends on it yet) |
| **G7 Shadow sync** | included | RB 7: `kubectl create -f infra/kubernetes/external-secrets/ml-platform.yaml`; wait SecretStore and ExternalSecret Ready; `shadow_compare.sh` | `SHADOW_MATCH`, exit 0; `feast-online-redis` resourceVersion unchanged | Any other status: stop; the consumer is unchanged |
| **G8 Consumer switch** | **[OWNER] approve the production release change** | `$P --mode consumer-switch` (must be READY); record the release revision; RB 8 `helm upgrade ewallet-serving … --reuse-values -f values-vault-eso.yaml` | rollout complete; Deployment references `feature-api-redis-vault`; no `EWALLET_VAULT_*` | Preflight not READY: do not upgrade. Readiness fails: RB 9 |
| **G9 Business E2E** | included | the §4 call with `GATE=g9`, same fixture | a §4 G9 class other than `REGRESSION_AFTER_MIGRATION` | 503/504/timeout: one retry after 30 s; then `REGRESSION_AFTER_MIGRATION`: RB 9 rollback, confirm the G1 class, stop |
| **G10 Negative tests** | **[OWNER] approve the test objects** | §5: precheck, `create` from `negative-tests.yaml`, causes, E8a/E8b, label cleanup | three expected causes; three target Secrets NotFound; nothing left with the label; `feature-api-redis` still `SecretSynced` | A target Secret appears or a cause differs: **stop**, delete the test objects, RB 9, investigate |
| **G11 Rollback drill** | **[OWNER] approve** | §4 *G11 rollback drill*: RB 9 → `g11-rollback` call → RB 8 → `g11-reupgrade` call; then §4 *Final state after G11* | rollback call in the G1 class, re-upgrade call in the G9 class; every final-state check holds; Vault PVCs untouched | re-upgrade fails: RB 9, confirm the G1 class, **`CAMPAIGN_B_FAILED_AT_G11`**, go to G12 |
| **G12 Sleep** | **[OWNER] approve the guarded sleep, KEDA pause, and each `--also-approve` target for this `$CID`** | §6 sequence | `verify --expect sleep` passes; 0 VMs; Vault and ESO still installed; both PVCs Bound, PVs `Retain`; no LoadBalancer created | `SLEEP_BLOCKED`: report it; never add flags or targets without approval |

An approved restart test (sealed → manual unseal → sync resumes) is optional evidence (O2). It
needs its own owner approval and must never be done by rotating the real Redis password.

### Capture checkpoints

The campaign pauses at these points until the owner has taken the screenshots (§7). Nothing is
reconstructed afterwards.

| After | Pause for | Then |
|---|---|---|
| G4 `describe` (root still valid) | E3 | `operator-token`, `retire-root` |
| G4 complete (unsealed) | E1, E2a, E2b | G5 |
| G5 | E3b | G6 |
| G6 | E4 | G7 |
| G7 `SHADOW_MATCH` | E5a, E5b | G8 (owner approval) |
| G8 rollout | E6 | G9 |
| G9 verdict | E7 (Swagger) | G10 |
| G10 rejections, **before deleting the test objects** | E8a, E8b | G10 cleanup |
| before G12 | checklist: E1–E8 present, each checked against §7 *Never visible* | owner approves the sleep |
| G12 verified | E9a, E9b | done |

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

**What the codes mean** ([`feast_online.py`](../../../ai_platform/inference/feast_online.py)): HTTP 424
(`missing_feature`, `stale_feature`, `invalid_feature_timestamp`) only follows a successful Redis read
that returned no or old values. Any Redis connection or authentication failure is 503
(`feature_backend_error`), a timeout 504 (`feature_timeout`). `/health/ready` runs a real Redis read
per feature view, so a wrong credential fails readiness. A credential problem therefore never
appears as a 424.

**Call (G1, G9, G11)**, the same fixture each time, through a local port-forward. `$GATE` is
`g1`, `g9`, `g11-rollback` or `g11-reupgrade`. The body holds feature values from the synthetic
dataset and error codes, never a credential:

```bash
kubectl --context $CTX -n ml-platform port-forward svc/feature-api 18080:80 >/dev/null 2>&1 &
code=$(curl -sS --max-time 30 -o ~/ewallet-evidence/vault/$CID/lookup-$GATE.json -w '%{http_code}' \
  -H 'content-type: application/json' \
  --data-binary @docs/evidence/edai2/keda/g5b_20261107T204930Z/validation/smoke/lookup-request.json \
  http://127.0.0.1:18080/v1/features/lookup); rc=$?
ready=$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/health/ready); rrc=$?
jq -cn --arg g "$GATE" --arg c "$code" --argjson e "$rc" --arg r "$ready" --argjson re "$rrc" \
  --slurpfile b ~/ewallet-evidence/vault/$CID/lookup-$GATE.json \
  '{gate: $g, http: $c, curl_exit: $e, ready_http: $r, ready_curl_exit: $re,
    features: (($b[0].features // {}) | keys), error_code: $b[0].error.code}' \
  | tee -a ~/ewallet-evidence/vault/$CID/lookup-results.jsonl
```

(`--slurpfile` fails on an empty body; then record `curl_exit` and `http` by hand: that case is
never a pass.)

**G1 baseline class** (decides whether the campaign may continue):

| G1 class | Condition | Next |
|---|---|---|
| `BASELINE_OK` | `curl_exit 0`, `http 200`, the 5 fixture feature names, `ready_http 200` | continue |
| `BASELINE_DATA_UNAVAILABLE` | `curl_exit 0`, `http 424` with `error_code` `missing_feature`, `stale_feature` or `invalid_feature_timestamp`, and `ready_http 200` | continue; G9 can at most prove the Secret integration |
| `BASELINE_UNHEALTHY` | anything else: 503, 504, another 5xx, 422, `curl_exit` ≠ 0 (timeout, no connection), `http 000`, or readiness ≠ 200 | **stop before G2**; nothing has changed yet; go to G12 |

**G9 verdict classes** (exactly one; HTTP 424 is never a business success). A 503, 504, timeout or
no response is retried once after 30 s; if it persists it is a regression, never a pass:

| Class | Condition |
|---|---|
| `BUSINESS_LOOKUP_PASS` | G1 `BASELINE_OK`, and G9 `curl_exit 0`, `http 200`, the same 5 feature names, `ready_http 200` |
| `SECRET_INTEGRATION_PASS_BUSINESS_LOOKUP_UNAVAILABLE` | G1 `BASELINE_DATA_UNAVAILABLE`, and G9 the same 424 `error_code` with `ready_http 200` on the switched pod (readiness opens Redis with the new Secret). The Secret integration is proven; a business lookup is not claimed |
| `REGRESSION_AFTER_MIGRATION` | anything else: worse than G1 (200 → 424), any 5xx, `curl_exit` ≠ 0, readiness ≠ 200. Roll back (RB 9), confirm the G1 class is back, then stop |

**G11 rollback drill.** Record the revision, roll back (RB 9), wait for the rollout, run the call
with `GATE=g11-rollback`; it must give the G1 class. Then run RB 8 again (including
`$P --mode consumer-switch`), wait for the rollout, and run the call with `GATE=g11-reupgrade`;
it must give the G9 class. If the re-upgrade does not reach the G9 class: RB 9 again, confirm the
G1 class on `feast-online-redis` (the safe state), record **`CAMPAIGN_B_FAILED_AT_G11`**, leave
Vault and ESO installed, and go to G12. Never retry the switch without a diagnosis.

### Final state after G11 (READ-ONLY; must all hold before G12)

| Check | Command | Expected |
|---|---|---|
| Consumer | `kubectl --context $CTX -n ml-platform get deploy feature-api -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="EWALLET_REDIS_CONNECTION_STRING")].valueFrom.secretKeyRef.name}'` | `feature-api-redis-vault` |
| Store and sync | `kubectl --context $CTX -n ml-platform get secretstore/vault externalsecret/feature-api-redis` | `Valid` / `SecretSynced`, Ready `True` |
| Readiness and lookup | the §4 call with `GATE=final` | `ready_http 200`; the G9 class |
| Existing Secret | `kubectl --context $CTX -n ml-platform get secret feast-online-redis -o jsonpath='{.metadata.resourceVersion}'` | equal to G1 |
| No test objects | `kubectl --context $CTX -n ml-platform get externalsecret,secretstore,serviceaccount -l ewallet.io/vault-campaign=$CID` | `No resources found` |
| Other releases | the command below | only `ml-platform/ewallet-serving` (three new revisions: G8, G11 rollback, G11 re-upgrade), `ewallet-vault/vault` and `external-secrets/external-secrets` |

```bash
cd ~/ewallet-evidence/vault/$CID && helm --kube-context $CTX list -A -a -o json > helm-final.json
jq -n --slurpfile a helm-g1.json --slurpfile b helm-final.json \
  '[$a[0][], $b[0][]] | group_by(.namespace + "/" + .name)
   | map(select(length != 2 or .[0].revision != .[1].revision)) | map(.[0].namespace + "/" + .[0].name)'
```

Any difference from the expected list or table: stop, record it, and explain it before G12.

## 5. G10 negative tests: three cases, created and removed by label

Campaign A proved six denial classes on GKE with synthetic values
([report](../../evidence/edai2/vault/gke_rehearsal_vault-rehearsal-20261124T173156Z/README.md)). G10 repeats the
three that matter for the real store, in `ml-platform`, with the committed template
[`negative-tests.yaml`](../../../infra/kubernetes/external-secrets/negative-tests.yaml): a throwaway
ServiceAccount `vault-neg-intruder`, SecretStores `vault-neg-wrong-sa` and `vault-neg-wrong-aud`,
and ExternalSecrets `vault-neg-agent-path`, `vault-neg-wrong-sa` and `vault-neg-wrong-aud`. Every
object carries `ewallet.io/vault-campaign=$CID`. Nothing is created in `ewallet-agents`; no existing
object is changed; the production `feature-api` ServiceAccount is not used.

| Case | ExternalSecret | Expected cause (ESO controller log) | Target Secret |
|---|---|---|---|
| Unauthorized path: the valid store `vault` asks for `agent-platform/agentgateway-client-key` | `vault-neg-agent-path` | `permission denied` | must not exist |
| Wrong ServiceAccount: `vault-neg-intruder` logs in to role `eso-ml-platform` | `vault-neg-wrong-sa` | `service account name not authorized` | must not exist |
| Wrong JWT audience: `eso-vault-reader` with audience `kubernetes` | `vault-neg-wrong-aud` | `invalid audience` | must not exist |

```bash
N="vault-neg-agent-path vault-neg-wrong-sa vault-neg-wrong-aud"
# 1. Precheck (READ-ONLY): must print nothing
for o in serviceaccount/vault-neg-intruder secretstore/vault-neg-wrong-sa secretstore/vault-neg-wrong-aud \
         $(for n in $N; do echo externalsecret/$n secret/$n; done); do
  kubectl --context $CTX -n ml-platform get $o -o name --ignore-not-found
done
# 2. Create (MUTATING, test objects only)
sed "s/__CID__/$CID/g" infra/kubernetes/external-secrets/negative-tests.yaml | kubectl --context $CTX create -f -
# 3. Each must fail, with its own cause, and leave no Secret
for n in $N; do kubectl --context $CTX -n ml-platform wait --for=condition=Ready=False externalsecret/$n --timeout=120s; done
logs=$(kubectl --context $CTX -n external-secrets logs deploy/external-secrets --since=15m)
for pair in "vault-neg-agent-path:permission denied" "vault-neg-wrong-sa:service account name not authorized" \
            "vault-neg-wrong-aud:invalid audience"; do
  n=${pair%%:*}; want=${pair#*:}
  got=$(grep -F "\"$n\"" <<<"$logs" | grep -oE 'permission denied|service account name not authorized|invalid audience' | sort -u | paste -sd,)
  [ "$got" = "$want" ] && echo "$n: $want" || echo "$n: MISSING or WRONG cause (got: ${got:-none})"
done
kubectl --context $CTX -n ml-platform get secret $N          # three times NotFound
# -> pause: E8a, E8b
# 4. Cleanup (MUTATING): only this campaign's objects, then prove nothing is left
kubectl --context $CTX -n ml-platform delete externalsecret,secretstore,serviceaccount -l ewallet.io/vault-campaign=$CID --wait=true
kubectl --context $CTX -n ml-platform get externalsecret,secretstore,serviceaccount,secret -l ewallet.io/vault-campaign=$CID   # No resources found
kubectl --context $CTX -n ml-platform get secret $N --ignore-not-found                                                         # nothing
```

PASS: the precheck prints nothing; each ExternalSecret reaches Ready `False`; the loop prints
each name with exactly its expected cause (the log lines naming it contain no other cause); all three target Secrets are NotFound; cleanup
leaves nothing with the label. **Any target Secret that appears, or a missing or different
cause, is a failure:** capture it, delete the test objects, roll the consumer back (RB 9) and
stop. An unrelated failure (for example Vault unreachable or sealed) never counts as a denial.
The real `ExternalSecret/feature-api-redis` stays `SecretSynced` throughout; check it after cleanup.

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

## 7. Visual evidence playbook (owner screenshots)

The owner takes every screenshot personally; none is generated or reconstructed. Interfaces are
the ones the project already has: the Google Cloud Console (GKE Workloads, Storage, Object
Browser, Logs Explorer, Compute Engine Disks), the Feature API's own Swagger UI through a
port-forward, and sanitized terminal output where no console view exists. No new tool is
installed: no k9s, no Vault UI (disabled in `values-gke.yaml`; it would need a browser login with
a privileged token), no public endpoint, no new dashboard. Grafana is not used: no Vault or ESO
metrics are scraped today.

Accepted files go to `docs/evidence/edai2/vault/gke_$CID/screenshots/`, then
`sha256sum *.png > SHA256SUMS.txt`. Console URLs are for project `ewallet-data-ai-platform-hg26`,
cluster `ewallet-dev`, zone `asia-southeast1-b`.

**Never visible, in any screenshot:** an unseal share or `vault-keys.json`, the root, operator or
admin token, a ServiceAccount JWT, the Redis password or `connection_string` value, any decoded
Secret, a Secret's YAML or *Data* tab in the Console (base64 is the value), a full environment
listing, a private key. Clear the terminal scrollback before each terminal capture. A screenshot
that shows a credential is rejected and the credential is treated as exposed: rotate it (for
Redis, the coordinated rotation outside this campaign) before any further evidence.

| ID | File | Required | Capture at | Pause before cleanup |
|---|---|---|---|---|
| E1 | `vault_01_gke_workload.png` | yes | G4 complete | no |
| E2a | `vault_02a_pvc_bound.png` | yes | G4 complete | no |
| E2b | `vault_02b_persistent_disks.png` | yes | G4 complete | no |
| E3 | `vault_03_kv_auth_policies.png` | yes | G4, after `describe`, **before `retire-root`** | yes: `describe` needs the initial root |
| E3b | `vault_03b_kv_metadata.png` | yes | G5 | no |
| E4 | `vault_04_eso_workloads.png` | yes | G6 | no |
| E5a | `vault_05a_externalsecret_synced.png` | yes | G7 | no |
| E5b | `vault_05b_shadow_match.png` | yes | G7 | no |
| E6 | `vault_06_feature_api_secret_ref.png` | yes | G8 | no |
| E7 | `vault_07_swagger_lookup.png` | yes | G9 | no |
| E8a | `vault_08a_denied_logs.png` | yes | G10 | **yes: before deleting the test objects** |
| E8b | `vault_08b_denied_status.png` | yes | G10 | **yes** |
| E9a | `vault_09a_nodes_zero.png` | yes | G12 | no |
| E9b | `vault_09b_disks_retained.png` | yes | G12 | no |
| O1 | `vault_10_staged_rotation.png` | optional | G10/G11 | no |
| O2 | `vault_11_seal_unseal.png` | optional | G11, approved restart | no |

**E1 Vault workload on GKE.** Console → Kubernetes Engine → Workloads → filter *Namespace:
ewallet-vault* → `vault` (StatefulSet) → *Overview*. Expected: status OK, 1/1 pods, `vault-0`
Running, container image `hashicorp/vault:2.0.4@sha256:5be4…`. PASS: all four visible. The page
shows Pod readiness only, which is `True` even while sealed; the caption pairs it with the
`sealed: false` of E3. Hide: nothing secret is on this page; do not open the *YAML* tab of any
Secret. Doc: VAULT.md *Architecture*.

**E2a PersistentVolumeClaims.** Console → Kubernetes Engine → Storage → *Persistent Volume
Claims* → filter *Namespace: ewallet-vault*. Expected: `data-vault-0` 5 GiB and `audit-vault-0`
1 GiB, Bound, storage class `standard-rwo`. PASS: both Bound. Doc: VAULT.md *Architecture*.

**E2b Persistent Disks.** Console → Compute Engine → Storage → *Disks* → filter by the two PV
names recorded at G2 (`pvc-…`). Expected: two *Balanced persistent disk* in `asia-southeast1-b`,
5 and 1 GB, *In use by* a `gke-ewallet-dev-cpu-pool-…` node. Add, in the same capture or beside
it, the terminal line `kubectl --context $CTX get pv -o custom-columns=PV:.metadata.name,RECLAIM:.spec.persistentVolumeReclaimPolicy,CLAIM:.spec.claimRef.name | grep vault-0`
showing `Retain` twice. PASS: two disks, zone b, reclaim `Retain`. Doc: VAULT.md *Architecture*
and *Operational limitations*.

**E3 KV v2, auth method, role and policy (live).** Terminal, port-forward open:
`$V status`, then `$V describe --keys-file $KEYDIR/vault-keys.json | jq '{secret_engines, auth_methods, audit_devices, role: .kubernetes_roles["eso-ml-platform"], policy: .acl_policies["eso-ml-platform"]}'`.
Expected: `version 2.0.4`, `initialized true`, `sealed false`; `ewallet/` type `kv` version `2`;
`kubernetes/` auth; `file/` audit; role `eso-ml-platform` bound to `eso-vault-reader` in
`ml-platform`, audience `vault`, TTL 300 s / 900 s, policy `eso-ml-platform`, no default
policy; the policy text with its single `read` path. PASS: every value as listed. Hide: these
commands print no secret; never `cat` the key file; clear the scrollback first (the `init` output
is safe, but nothing else from G3 may be on screen). Doc: VAULT.md *Kubernetes authentication,
roles and policies*.

**E3b Centrally stored secret, metadata only.** Terminal after G5:
`$V versions --token-file $OP --path feature-api/feast-redis`. Expected:
`{"current_version": 1, "updated_time": …}`, no value. PASS: version 1 and a time from this
campaign. Hide: never show the `kubectl get secret … | base64 -d | $V put` pipeline's input. Doc:
VAULT.md *Secret lifecycle*.

**E4 ESO on GKE.** Console → Kubernetes Engine → Workloads → filter *Namespace:
external-secrets*. Expected: `external-secrets`, `external-secrets-cert-controller`,
`external-secrets-webhook`, each OK 1/1, image `ghcr.io/external-secrets/external-secrets:v2.14.0@…`.
PASS: three OK. Doc: VAULT.md *External Secrets Operator*.

**E5a SecretStore and ExternalSecret status.** Console → Kubernetes Engine → *Object Browser* →
API group `external-secrets.io` → `ExternalSecret` → `ml-platform/feature-api-redis` → *YAML*,
scrolled to `status`: condition `Ready: True`, reason `SecretSynced`, `refreshTime`,
`syncedResourceVersion`, and `spec.target.name: feature-api-redis-vault`. Then the same for
`SecretStore` `ml-platform/vault`: `Ready: True`, reason `Valid`. These objects hold references,
never values. **Never open the `Secret` kind in the Object Browser.** PASS: both Ready True. Doc:
VAULT.md *SecretStore and ExternalSecret*.

**E5b Shadow equality without values.** Terminal:
`kubectl --context $CTX -n ml-platform get secretstore,externalsecret`,
`kubectl --context $CTX -n ml-platform get secret feature-api-redis-vault -o json | jq -c '.data | keys'`,
`bash scripts/vault/shadow_compare.sh $CTX ml-platform feast-online-redis feature-api-redis-vault; echo exit=$?`.
Expected: `Valid`/`SecretSynced`, `["connection_string"]`, `SHADOW_MATCH`, `exit=0`. PASS: all
four. Doc: VAULT.md *SecretStore and ExternalSecret*.

**E6 The Feature API reads the ESO-managed Secret.** Console → Kubernetes Engine → Workloads →
*Namespace: ml-platform* → `feature-api` → *YAML*, scrolled to `env` →
`EWALLET_REDIS_CONNECTION_STRING` → `valueFrom.secretKeyRef.name: feature-api-redis-vault`; and the
*Revision history* tab showing the new revision rolled out. Check before capturing that no env
entry on screen carries a literal credential `value:`. PASS: the reference and a completed
rollout. Doc: VAULT.md *Feature API integration*.

**E7 Business lookup through Swagger.** `kubectl --context $CTX -n ml-platform port-forward svc/feature-api 18080:80`,
browser `http://127.0.0.1:18080/docs` (the public edge serves only the lookup and returns 404 for
`/docs`) → `POST /v1/features/lookup` → *Try it out* → paste the body of
[`lookup-request.json`](../../evidence/edai2/keda/g5b_20261107T204930Z/validation/smoke/lookup-request.json)
→ *Execute*. Expected for `BUSINESS_LOOKUP_PASS`: code 200, the 5 feature names with values,
`oldest_feature_timestamp`, `correlation_id`. PASS: the G9 class from §4. If the class is
`SECRET_INTEGRATION_PASS_BUSINESS_LOOKUP_UNAVAILABLE`, the screenshot shows the same non-200 code
as G1 and the caption says so; HTTP 424 is never presented as a business success. Hide: nothing
secret (entity IDs come from the synthetic dataset). Doc: VAULT.md *Feature API integration*.

**E8a Unauthorized access denied, from the controller log.** Before deleting the G10 objects:
Console → Logging → *Logs Explorer*, last 30 minutes, query
`resource.type="k8s_container" resource.labels.cluster_name="ewallet-dev" resource.labels.namespace_name="external-secrets" "vault-neg-"`.
Expected: ESO error lines for `vault-neg-agent-path` (`permission denied`), `vault-neg-wrong-sa`
(`service account name not authorized`) and `vault-neg-wrong-aud` (`invalid audience`). The log
names the path and role, never a token. PASS: each name with its own cause. Doc: VAULT.md
*Namespace isolation*.

**E8b Denied status and no target Secret.** Terminal:
`kubectl --context $CTX -n ml-platform get externalsecret vault-neg-agent-path` (Ready `False`,
`SecretSyncedError`) and `kubectl --context $CTX -n ml-platform get secret vault-neg-agent-path`
(`NotFound`), and the same pair for `vault-neg-wrong-sa` and `vault-neg-wrong-aud` (the objects
of §5). PASS: three failure statuses and three NotFound. Doc: VAULT.md *Namespace isolation*.

**E9a Back to zero nodes.** After G12: Console → Kubernetes Engine → Clusters → `ewallet-dev` →
*Nodes*: node pool `cpu-pool`, 0 nodes. Optionally the terminal `cloud_lab.py verify --expect sleep`
result. PASS: 0 nodes. Doc: VAULT.md *Operational limitations*.

**E9b Vault data kept while asleep.** Console → Compute Engine → *Disks*, the same two `pvc-…`
disks: present, *In use by* empty; terminal `kubectl --context $CTX -n ewallet-vault get pvc`
(both Bound) and the `get pv … | grep vault-0` line (`Retain`). PASS: both disks present and
retained. Doc: VAULT.md *Operational limitations*.

**O1 Versioning and propagation (optional, staged agent path, synthetic).** After seeding a new
synthetic version of `agent-platform/agentgateway-client-key` (RB 5) and a force-sync of the staged
`ewallet-agents` ExternalSecret: `$V versions --token-file $OP --path agent-platform/agentgateway-client-key`
(version 2), and the ExternalSecret's changed `syncedResourceVersion`. Never with the real Redis
value. Doc: VAULT.md *Rotation and recovery*.

**O2 Seal, manual unseal, recovery (optional, approved restart).** `$V status` (sealed) after
`kubectl delete pod vault-0`, the Workloads page still showing the pod Ready, then `$V unseal`,
`$V status` (unsealed) and the ExternalSecret back to `SecretSynced`. Doc: VAULT.md *Rotation and
recovery*.

## 8. Documentation plan

| Document | After an accepted campaign |
|---|---|
| [VAULT.md](../../edai2/platform/VAULT.md) | `GKE_DEPLOYED` / `GKE_RUNTIME_VERIFIED` only for gates that passed; GKE environment; E1–E9 figures with captions; the G9 verdict class; limitations |
| [VAULT_ESO_RUNBOOK.md](VAULT_ESO_RUNBOOK.md) | corrections found during the run |
| `docs/evidence/edai2/vault/gke_$CID/CAMPAIGN_REPORT.md` | new: campaign ID, commit, environment, approved scope (incl. `--also-approve` targets), resource inventory, gate results, issues, rollback, evidence with SHA-256, final state, limitations. It follows the gateway evidence README ([g3b-owner-evidence-20261124T083106Z](../../evidence/edai2/gateway/g3b-owner-evidence-20261124T083106Z/README.md)) |
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
| G10–G11 | Approve the negative-test objects and the rollback drill; optionally the restart test (O2) |
| G4–G12 | Take screenshots E1–E9 at the capture checkpoints (§3) |
| G12 | Approve the guarded sleep, the KEDA pause and each `--also-approve` target for this campaign |
| After | Accept the evidence before any rubric row changes |
