# Vault and External Secrets Operator: deployment, migration and recovery (Vault V2)

Operating procedure for [VAULT.md](../../edai2/platform/VAULT.md). Each step is labelled:

- **READ-ONLY:** reads state; changes nothing.
- **MUTATING (Kubernetes):** creates or changes cluster objects.
- **MUTATING (Vault):** changes Vault state or secret data.
- **MUTATING (GCP):** creates or changes Google Cloud resources.
- **OWNER-ONLY:** handles recovery material; the owner runs it personally, at the laptop.

**Status:**

- Vault V2 is implemented on branch `experiment/edai2-vault-clustersecretstore`. That means one
  ClusterSecretStore, Cloud KMS auto-unseal, and three credential groups. The design is in
  [VAULT_V2_ARCHITECTURE_AUDIT.md](../../edai2/platform/VAULT_V2_ARCHITECTURE_AUDIT.md).
- It is verified end to end on **kind only**, with auto-unseal **simulated** by a local transit seal
  (VAULT.md *Validation*).
- **Nothing of V2 is deployed on GKE, and the Cloud KMS resources do not exist yet.** Their Terraform, plan guard
  and preflight are written and tested offline (P4-A); creating them is P4-B (step 0).
  Every GKE step needs a separately approved campaign. GKE is woken and put back to sleep only by
  that campaign's approved cloud-lab procedure, never by a command from this page.

Never print, paste or commit a Vault token, a recovery key, a private key or a secret value.
Do not use `set -x`. Values go into Vault on stdin (`put --stdin`) or a hidden prompt, never as an argument.

## Variables

```bash
# The cloud-lab kubeconfig reaches the control plane through its DNS endpoint; the default
# ~/.kube/config uses the IP endpoint, which the master authorized networks block from the laptop.
export KUBECONFIG=~/.cache/ewallet-cloud-lab/kubeconfig
CTX=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev   # pin every command
KEYDIR=~/ewallet-evidence/vault/private      # outside the repository, mode 700; ops/vault refuses a path inside it
CHARTS=~/ewallet-evidence/vault/charts       # pulled charts, digests checked below
V="python3 -m ops.vault.cli --ca-file $KEYDIR/ca.crt"   # talks to https://127.0.0.1:8200 (port-forward)
OP=$KEYDIR/operator-token.json               # secret-operator token, created in step 4
```

## 0. Cloud KMS (P4-B: separate owner approval per step; NOT DONE)

`values-gke.yaml` configures `seal "gcpckms"`: project `ewallet-data-ai-platform-hg26`, region
`asia-southeast1`, key ring `ewallet-vault`, key `vault-unseal`. It has no credentials file: Vault gets
Application Default Credentials from Workload Identity. Until the KMS resources exist and pass the
preflight, the rehearsal driver's `vault` phase refuses on GKE and Vault could not start.

**Implemented and offline-tested (P4-A), not applied:**

- **Module** [`modules/vault_kms`](../../../infra/terraform/gcp/modules/vault_kms/main.tf), called from
  `environments/dev` as `module.vault_kms` with `enable_vault_kms` (default `false`). It has no
  `depends_on` and no input from `module.gke` or `module.workload_identity`.
- **Plan guard** [`ops/gcp/vault_kms_terraform.py`](../../../ops/gcp/vault_kms_terraform.py): exactly seven
  creates, each with its expected attributes, and nothing else anywhere in the plan.
- **Preflight** [`scripts/vault/kms_preflight.py`](../../../scripts/vault/kms_preflight.py): read-only.

| Address (`module.vault_kms.…[0]`) | What | Protection |
|---|---|---|
| `google_project_service.cloudkms` | Cloud KMS API | never disabled (`disable_on_destroy = false`, `deletion_policy = "ABANDON"`) |
| `google_kms_key_ring.vault` | key ring `ewallet-vault`, `asia-southeast1` | `prevent_destroy`; Cloud KMS cannot delete key rings |
| `google_kms_crypto_key.vault_unseal` | key `vault-unseal`, `ENCRYPT_DECRYPT`, SOFTWARE, **no rotation**, 30-day destroy delay | `prevent_destroy` + `deletion_policy = "PREVENT"` |
| `google_service_account.vault_unseal` | GSA `ewallet-vault-unseal`, no key ever created | `prevent_destroy` + `deletion_policy = "PREVENT"` |
| `google_kms_crypto_key_iam_member.vault_encrypter_decrypter` | `roles/cloudkms.cryptoKeyEncrypterDecrypter` **on the key** | non-authoritative member |
| `google_kms_crypto_key_iam_member.vault_viewer` | `roles/cloudkms.viewer` **on the key** (`cryptoKeys.get`) | non-authoritative member |
| `google_service_account_iam_member.vault_workload_identity` | `roles/iam.workloadIdentityUser` for `…svc.id.goog[ewallet-vault/vault]` **on the GSA** | non-authoritative member |

`prevent_destroy` and `deletion_policy = "PREVENT"` stop only Terraform. Neither stops a console, `gcloud` or
API action by a principal with KMS admin rights. A destroyed key version is unrecoverable, so the key's
safety also rests on IAM: no automation holds destroy rights.

**P4-B procedure.** Each step is a stop point; nothing goes on automatically.

1. **Read-only discovery** (owner approves cloud access). **Done: P4-B1,
   [`p4b1_cloud_discovery_20261125`](../../evidence/edai2/vault/p4b1_cloud_discovery_20261125/README.md),
   `VAULT_V2_P4B1_READY_WITH_WARNINGS`.** Discovery is not readiness.
   - `kms_preflight.py --mode cloud` answers "is everything ready?" **after** provisioning. Before it, its
     FAIL or BLOCKED never proves that something is absent.
   - Discovery needs authoritative answers:
     - **Cloud KMS API:** a successful `gcloud services list --enabled` filter, plus the Service Usage audit
       history (with a positive control on another API).
     - **Key ring and key:** while the API is disabled the KMS API answers 403 SERVICE_DISABLED (not
       absence). The Cloud KMS Admin Activity history (400-day retention) must show no `CreateKeyRing` or
       `CreateCryptoKey`.
     - **GSA:** IAM NOT_FOUND.
     - **State:** `terraform state list` (addresses only, no lock, no refresh; the object generation
       unchanged) must show no `module.vault_kms` and no `google_kms_*` anywhere.
     - **GKE:** the cluster's `workloadIdentityConfig` read through the management API, never kubectl.
   - Any existing key ring, key or GSA, or any competing state address, means **stop for an ownership or
     import decision**: never adopt or overwrite it.
2. **Saved plan** (done: P4-B2,
   [`p4b2_real_plan_20261125`](../../evidence/edai2/vault/p4b2_real_plan_20261125/README.md): 7 creates,
   `VAULT_KMS_PLAN_GUARD_PASS`, plan SHA-256 `19c7f9d8…68df77`, kept privately; **not applied**) with the real backend:
   `terraform plan -target=module.vault_kms -var enable_vault_kms=true -out=p4.tfplan`, plus the tfvars the
   state was applied with (`ops.gcp.r2_terraform tfvars`). `-target` alone is **not** proof of a narrow
   plan; the guard decides.
3. **Guard**: `terraform show -json p4.tfplan > p4.plan.json` and
   `python3 -m ops.gcp.vault_kms_terraform guard --plan p4.plan.json`. Only `VAULT_KMS_PLAN_GUARD_PASS`
   continues. Any other action (for example the node-pool replacement the drifted state produces in a full
   plan), an import or a drifted KMS address stops.
4. **Owner review:**
   - the seven addresses and three IAM grants;
   - 7 creates, 0 updates, 0 deletes;
   - the drift the guard lists outside the change;
   - cost: about US$0.06 a month per active key version, while asleep too;
   - recovery: the key can never be deleted.
5. **Approved apply of that exact file:** `terraform apply p4.tfplan`, with no re-plan. Then
   `kms_preflight.py --mode cloud --vault-chart …` must be `VAULT_KMS_PREFLIGHT_READY`. Record its gates.
6. **Stop.** P4-B never wakes GKE; Campaign A2 is its own approval.

If the apply fails part-way, do not run destroy, `state rm` or a re-apply. Inspect what exists (preflight,
state list) and decide deliberately. No step here deletes or disables a key, a key version or the GSA.

The offline record is in [`p4a_terraform_offline_20261125`](../../evidence/edai2/vault/p4a_terraform_offline_20261125/README.md).

## 1. Prerequisites (READ-ONLY)

Run the committed preflight first. It checks every item below that it can, and runs
only read-only commands. A query that fails or returns unreadable data is FAIL, never PASS:

```bash
python3 scripts/vault/gke_preflight.py --mode preinstall --kubeconfig $KUBECONFIG --context $CTX --out <campaign-dir>/preflight
# last line: {"mode": "preinstall", "verdict": "VAULT_GKE_PREFLIGHT_READY", "blocking": [], "not_checked": [...]}
```

Before a consumer switch (step 8), run `--mode consumer-switch` instead. It expects Vault and ESO
to exist, and checks:

- the synced `ml-platform/ExternalSecret/feast-online-redis` and its ESO-owned target `feast-online-redis-vault`;
- `SHADOW_MATCH`;
- the current Feature API;
- the release drift and the overlay scope.

The Cloud KMS gates (key enabled, primary version enabled, the three IAM bindings) are part of P4
and not implemented yet. Until they are, check step 0 by hand, read-only.

The preflight refuses (BLOCKED) if any of these is true:

- an earlier Vault or ESO namespace, CRD, release or PVC exists;
- the `ewallet-serving` release differs from the repository chart;
- the overlay would change anything besides the Feature API Secret name.

While the node pool is at 0, `cert-manager-ready` stays NOT_CHECKED; rerun the preflight after the wake.

- Approved campaign, GKE awake, laptop on the approved network.
- cert-manager running: `kubectl --context $CTX get crd certificates.cert-manager.io && kubectl --context $CTX -n cert-manager get pods`.
- No earlier installation: `kubectl --context $CTX get ns ewallet-vault external-secrets` returns NotFound for both.
  **No Raft data initialised under Shamir may be reused with the gcpckms configuration.** That would need the
  official seal migration (audit §8.6); this procedure never does it.
- The Secrets being shadowed exist (names only): `kubectl --context $CTX -n ml-platform get secret feast-online-redis -o name`,
  `kubectl --context $CTX -n ewallet-agents get secret vllm-api-key agentgateway-client-key -o name`.
- StorageClass `standard-rwo` exists (Vault data 5 GiB and audit 1 GiB Persistent Disks).
- Charts, digests checked:

  ```bash
  mkdir -p $CHARTS
  helm pull vault --repo https://helm.releases.hashicorp.com --version 0.34.1 -d $CHARTS
  helm pull external-secrets --repo https://charts.external-secrets.io --version 2.14.0 -d $CHARTS
  sha256sum $CHARTS/*.tgz
  # df4c37fac32496892ff1e1d6fd7954d3a402cc7b1b2b4c2226af11b8a57564c4  vault-0.34.1.tgz
  # 0a6d9e91b1ff50adfdbf24b50b582babfa7c5b7df42b21974ca4b3fb88ee5e34  external-secrets-2.14.0.tgz
  ```

- `KEYDIR` exists with mode 700, plus two offline encrypted media for the recovery file (step 3).

## 2. Vault server (MUTATING, Kubernetes)

```bash
kubectl --context $CTX create -f infra/kubernetes/vault/namespace.yaml -f infra/kubernetes/vault/certificate.yaml
kubectl --context $CTX -n ewallet-vault wait --for=condition=Ready certificate/vault-server-tls --timeout=120s
helm --kube-context $CTX install vault $CHARTS/vault-0.34.1.tgz -n ewallet-vault -f infra/kubernetes/vault/values-gke.yaml
kubectl --context $CTX create -f infra/kubernetes/vault/network-policy.yaml -f infra/kubernetes/vault/admin-serviceaccount.yaml
kubectl --context $CTX -n ewallet-vault wait --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=240s
kubectl --context $CTX -n ewallet-vault get sa vault -o jsonpath='{.metadata.annotations.iam\.gke\.io/gcp-service-account}'   # ewallet-vault-unseal@…
kubectl --context $CTX -n ewallet-vault get pvc        # READ-ONLY: data-vault-0 (5Gi) and audit-vault-0 (1Gi) Bound
kubectl --context $CTX -n ewallet-vault port-forward pod/vault-0 8200:8200 &   # keep open; restart it after a Vault restart
kubectl --context $CTX -n ewallet-vault get secret vault-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > $KEYDIR/ca.crt
$V status        # READ-ONLY: {"initialized": false, "sealed": true, ..., "seal_type": "gcpckms", "recovery_seal": true}
```

`seal_type` must be `gcpckms`. If Vault does not start (`kubectl logs vault-0`: `error parsing Seal
configuration`), it cannot reach or use the key. Stop and check step 0; never fall back to Shamir.

**PersistentVolume retention (recommended, owner approval, before step 3).** `standard-rwo` has
`reclaimPolicy: Delete`: deleting a Vault PVC, or the `ewallet-vault` namespace, deletes the
Persistent Disk and every secret on it. Set both Vault PVs to `Retain` while they are still empty
(**MUTATING**, reversible):

```bash
kubectl --context $CTX -n ewallet-vault get pvc -o custom-columns=PVC:.metadata.name,PV:.spec.volumeName   # READ-ONLY: record both PV names
for pv in $(kubectl --context $CTX -n ewallet-vault get pvc -o jsonpath='{.items[*].spec.volumeName}'); do
  kubectl --context $CTX patch pv "$pv" -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
done
kubectl --context $CTX get pv -o custom-columns=PV:.metadata.name,RECLAIM:.spec.persistentVolumeReclaimPolicy,CLAIM:.spec.claimRef.name | grep vault-0   # Retain, Retain
```

- **Protects against:** deleting a Vault PVC or the namespace. The PV becomes `Released` and the disk stays.
- **Does not protect against:**
  - deleting the disk itself;
  - deleting data inside Vault;
  - corruption;
  - losing the Cloud KMS key: without it a retained disk is unreadable.
- **Recovery of a Released PV** (owner approval):
  1. `kubectl --context $CTX patch pv <pv> --type json -p '[{"op":"remove","path":"/spec/claimRef"}]'`;
  2. recreate PVC `data-vault-0` (or `audit-vault-0`) in `ewallet-vault` with `storageClassName:
     standard-rwo`, the original size and `volumeName: <pv>`.

  Vault then starts and unseals itself through Cloud KMS. Proven on kind with the transit seal:
  VAULT.md *Validation*, phase `retain`.
- **Permanent deletion** needs its own owner decision, after a Raft snapshot.

**Readiness.** An uninitialised Vault is Ready (`uninitcode=204`), so the bootstrap can proceed.
After `init`, **Ready means unsealed**: an initialised but sealed Vault answers 503 and is NotReady.
V1 was different: it treated sealed as Ready because every wake sealed it.

## 3. Initialise (MUTATING, Vault; OWNER-ONLY)

```bash
$V init --keys-out $KEYDIR/vault-keys.json     # auto-unseal: 3 RECOVERY keys, threshold 2; refuses an existing file or a path in the repo
$V status                                      # within a minute: initialized true, sealed false, seal_type gcpckms (no unseal command)
```

`init` reads the seal type first:

- under `gcpckms` it requests `recovery_shares=3, recovery_threshold=2` and stores
  `recovery_keys_base64` together with the seal type;
- under Shamir (V1) it would store unseal keys.

Safety rules:

- The file is created exclusively, mode 0600, outside the repository, **before** Vault is asked,
  and forced to disk before success is reported.
- `REFUSED vault_initialised_keys_not_stored`, `vault_init_outcome_unknown` or
  `vault_init_shares_unexpected` all mean: **stop, never run `init` again**, check `$V status`
  (*Recovery* below).

`vault-keys.json` holds **the three recovery keys and the initial root token**:

1. Copy it at once to the two offline encrypted media. Never to cloud sync, chat, Git or evidence.
2. The root token inside it is used only for step 4, then revoked (*Token lifecycle*).
3. **Recovery keys never unseal Vault.** They only authorise the root-generation ceremony and
   rekeying. `$V unseal` refuses on an auto-unseal Vault (`REFUSED vault_auto_unseal`) and never
   submits a key.

## 4. Configure (MUTATING, Vault)

There is no unseal step: Vault unsealed itself in step 3.

```bash
$V status                                           # gate: initialized true, sealed false
$V configure --keys-file $KEYDIR/vault-keys.json    # audit, KV v2 "ewallet", Kubernetes auth, policies, roles
$V describe --keys-file $KEYDIR/vault-keys.json     # READ-ONLY: live engines, auth, audit, roles, policy text; no secret data
$V operator-token --keys-file $KEYDIR/vault-keys.json --token-out $OP   # orphan, policy secret-operator, TTL 720h
$V retire-root --keys-file $KEYDIR/vault-keys.json                      # {"root_token_revoked": true}
$V configure --keys-file $KEYDIR/vault-keys.json    # must now fail with "returned 403"
```

`configure` applies [bootstrap.json](../../../infra/kubernetes/vault/bootstrap.json) idempotently:

| Vault role | Bound identity | Policy |
|---|---|---|
| `eso-ewallet-reader` | `external-secrets/eso-vault-reader`, audience `vault`, 5 min | read `ewallet/data/ml-platform/*` and `ewallet/data/agent-platform/*`; no list, metadata, write or `sys/` |
| `vault-admin` | `ewallet-vault/vault-admin` (the owner's admin login), 15 min | nothing: policy `root-generation` only |

Retired in V2: the per-namespace readers `eso-ml-platform` and `eso-ewallet-agents`, and the
direct-loader roles `feature-api` and `fraud-api`.

## 5. Seed Vault from the existing Secrets (MUTATING, Vault)

Each value goes from the existing Secret straight into Vault on stdin, without being printed. There is
one Vault record per underlying credential; the Feature API and the Fraud API share the first one.

```bash
kubectl --context $CTX -n ml-platform get secret feast-online-redis -o jsonpath='{.data.connection_string}' | base64 -d \
  | $V put --token-file $OP --path ml-platform/feast-online-redis --key connection_string --stdin
kubectl --context $CTX -n ewallet-agents get secret vllm-api-key -o jsonpath='{.data.api-key}' | base64 -d \
  | $V put --token-file $OP --path agent-platform/vllm-api-key --key api-key --stdin
kubectl --context $CTX -n ewallet-agents get secret agentgateway-client-key -o jsonpath='{.data.api-key}' | base64 -d \
  | $V put --token-file $OP --path agent-platform/agentgateway-client-key --key api-key --stdin
$V versions --token-file $OP --path ml-platform/feast-online-redis     # metadata only: current_version 1
```

Each Agent Platform record is seeded only once its consumer switch (step 8b) is approved.

## 6. External Secrets Operator and the shared store (MUTATING, Kubernetes)

```bash
kubectl --context $CTX create -f infra/kubernetes/external-secrets/namespace.yaml
helm --kube-context $CTX install external-secrets $CHARTS/external-secrets-2.14.0.tgz -n external-secrets \
  -f infra/kubernetes/external-secrets/values.yaml --wait --timeout 5m
kubectl --context $CTX -n external-secrets get deploy      # READ-ONLY: 3 deployments, 1/1 each
bash scripts/vault/publish_ca.sh $CTX                      # CA_PUBLISHED: external-secrets/vault-ca (ca.crt only)
kubectl --context $CTX create -f infra/kubernetes/external-secrets/cluster-secret-store.yaml
kubectl --context $CTX wait --for=condition=Ready clustersecretstore/ewallet-vault --timeout=120s
```

ESO configuration:

- **Processed:** namespaced SecretStores, and the one ClusterSecretStore.
- **Off:** ClusterExternalSecret, PushSecret and ClusterPushSecret (`values.yaml`). ESO never writes to Vault.
- **Who may use `ewallet-vault`:** only ExternalSecrets in `ml-platform` and `ewallet-agents` (exact names).
  The store logs in as `external-secrets/eso-vault-reader`, an identity with no pod, no RBAC and no
  mounted token.
- **Network:** Vault's NetworkPolicy admits the ESO controller pod only.
- **`publish_ca.sh`:** never overwrites a ConfigMap that it did not publish.

## 7. Shadow synchronisation (MUTATING, Kubernetes)

ESO creates **new** Secrets. Every consumer still reads its original Secret.

```bash
kubectl --context $CTX create -f infra/kubernetes/external-secrets/ml-platform.yaml   # create: fails rather than overwrites
kubectl --context $CTX -n ml-platform wait --for=condition=Ready externalsecret/feast-online-redis --timeout=120s
bash scripts/vault/shadow_compare.sh $CTX ml-platform feast-online-redis feast-online-redis-vault
# Agent Platform (with step 8b only):
kubectl --context $CTX create -f infra/kubernetes/external-secrets/ewallet-agents.yaml
bash scripts/vault/shadow_compare.sh $CTX ewallet-agents vllm-api-key vllm-api-key-vault api-key
bash scripts/vault/shadow_compare.sh $CTX ewallet-agents agentgateway-client-key agentgateway-client-key-vault api-key
```

[`shadow_compare.sh`](../../../scripts/vault/shadow_compare.sh) exits 0 only with
`SHADOW_MATCH`: both Secrets exist, both have a non-empty value under the key, every kubectl,
jq and base64 step succeeded, and the values are equal. Anything else prints its own status and
exits 1: `SHADOW_MISMATCH`, `SHADOW_<EXISTING|SHADOW>_SECRET_MISSING`, `…_KEY_MISSING`,
`…_VALUE_EMPTY` or `…_READ_FAILED`. It prints one status, never a value or a hash.

**Gate:** proceed only on `SHADOW_MATCH` with exit code 0, for every pair being switched.

## 8. Consumer switch (MUTATING, Kubernetes; production releases, one group at a time)

**8a. Feature API, then Fraud API** (the same Secret `feast-online-redis-vault`):

```bash
python3 scripts/vault/gke_preflight.py --mode consumer-switch --kubeconfig $KUBECONFIG --context $CTX   # must end READY
helm --kube-context $CTX -n ml-platform history ewallet-serving --max 1      # note the revision for rollback
helm upgrade ewallet-serving infra/helm/ewallet-serving --kube-context $CTX -n ml-platform --reuse-values \
  -f infra/helm/ewallet-serving/values-vault-eso.yaml
kubectl --context $CTX -n ml-platform rollout status deployment/feature-api --timeout=300s
# after one real lookup (acceptance plan §4):
kubectl --context $CTX -n ml-platform patch deployment fraud-api --type=strategic \
  --patch-file infra/kubernetes/fraud-serving/fraud-api-eso.patch.yaml
kubectl --context $CTX -n ml-platform rollout status deployment/fraud-api --timeout=300s
```

**8b. Agent Platform** (`vllm-api-key` and `agentgateway-client-key`, one upgrade):

```bash
helm --kube-context $CTX -n <llm-agent release namespace> history llm-agent --max 1      # note the revision
helm upgrade llm-agent infra/helm/llm-agent-platform --kube-context $CTX -n <namespace> --reuse-values \
  -f infra/helm/llm-agent-platform/values-vault-eso.yaml
```

The overlay moves the backend (`vllm.apiKeySecret`), the validator (`agentgateway.clientKeySecret`)
and every client (`modelConfig.apiKeySecret`: ModelConfig and the BYO agents) together.

- **Why it is safe:** the values are identical (`SHADOW_MATCH`), so the rollout changes nothing at
  runtime.
- **What it leaves untouched:** the MCP credential releases (`mcp-token-*`, `mcp-auth-*`) and the
  Coordinator idempotency Secret (tested).
- **Afterwards:** check a gateway call (200 with the key, 401 wrong or missing), and kagent chat.

The overlays keep the pods away from Vault: no Vault token, address or CA in any pod.

## 9. Rollback (MUTATING, Kubernetes)

```bash
helm --kube-context $CTX -n ml-platform rollback ewallet-serving <REVISION-FROM-STEP-8a>
kubectl --context $CTX -n ml-platform rollout undo deployment/fraud-api
helm --kube-context $CTX -n <namespace> rollback llm-agent <REVISION-FROM-STEP-8b>
```

Rollback rules:

- The original Secrets are never changed by this procedure, so rollback restores the exact earlier state.
- Leave the ExternalSecrets in place, or delete them after rollback: each owns its `*-vault` Secret,
  and deleting it deletes that Secret.
- Never delete an original Secret.
- After a real rotation (step 11), the original Secret holds the retired credential and is no longer
  a valid rollback target.

## 10. Verification after deployment (READ-ONLY, except the negative test objects)

| Check | Command | Expected |
|---|---|---|
| Vault runtime | `$V status` | `initialized: true`, `sealed: false`, `seal_type: gcpckms` |
| KV metadata | `$V versions --token-file $OP --path ml-platform/feast-online-redis` | version and time, no value |
| ACL | `describe` output (step 4) | `eso-ewallet-reader`: two `read` prefixes plus the two self-token paths |
| Store | `kubectl get clustersecretstore ewallet-vault` | Ready `True`, `Valid` |
| Sync | `kubectl -n ml-platform get externalsecret feast-online-redis` | `SecretSynced`, Ready `True` |
| Consumers | `secretKeyRef` of `deployment/feature-api` and `deployment/fraud-api`, one lookup | `feast-online-redis-vault`, HTTP 200 |
| Denied access | create the labelled test objects from [`negative-tests.yaml`](../../../infra/kubernetes/external-secrets/negative-tests.yaml) (`sed "s/__CID__/$CID/g" … \| kubectl create -f -`), then read each cause in `kubectl -n external-secrets logs deploy/external-secrets` (ESO events are generic); procedure in [VAULT_GKE_ACCEPTANCE_CAMPAIGN.md §5](VAULT_GKE_ACCEPTANCE_CAMPAIGN.md#5-g10-negative-tests-vault-v2-five-cases-created-and-removed-by-label) | `vault-neg-out-of-prefix` `permission denied`, `vault-neg-wrong-sa` `service account name not authorized`, `vault-neg-wrong-ns` `namespace not authorized`, `vault-neg-wrong-aud` `invalid audience`, `vault-neg-sandbox` store not allowed in the namespace; Ready `False`; no target Secret; afterwards `delete … -l ewallet.io/vault-campaign=$CID` leaves nothing |

## 11. Rotation (MUTATING, Vault and Kubernetes)

A new KV version reaches the target Secret within `refreshInterval` (1 h), or at once with
`kubectl … annotate externalsecret <name> force-sync=$(date +%s) --overwrite`. Every consumer
reads its credential at start-up, so each rotation ends with a restart:

- **Feast Redis:** change the server password first (Redis `CONFIG SET requirepass`, then the
  server's own Secret, `feature-store/redis`, which V2 does not manage). Then:
  1. `put` the new connection string;
  2. force-sync;
  3. restart the Feature API and the Fraud API.

  The original Secret now holds the retired credential. Rehearsed on kind with a synthetic Redis
  (phase `lifecycle`).
- **Gateway client key:** `put`, force-sync, then restart agentgateway **and** every agent that
  reads the ModelConfig. The gateway accepts one key, so clients that have not restarted are
  refused (rehearsed on kind: old key 200 until the gateway restart, 401 after).
- **vLLM backend key:** `put`, force-sync, then restart vLLM **and** agentgateway together.
  Restarting only one side breaks the pair (rehearsed on kind: the real vLLM refuses the other key).

Production rotation of the Agent Platform keys is deferred: it needs a coordinated restart window
(audit §13).

## Token lifecycle and later administration

Vault 2.0 requires authentication for root generation. This deployment keeps that default:
`values-gke.yaml` has **no** `enable_unauthenticated_access`.

| Credential | Created by | Used for | Ends |
|---|---|---|---|
| Recovery keys (in `vault-keys.json`) | `init` | completing a root-generation ceremony (2 of 3); never unsealing | never; offline copies |
| Initial root token (in `vault-keys.json`) | `init` | step 4 only | `retire-root --keys-file` at the end of step 4 |
| Operator token (`$OP`) | `operator-token` | `put` (create or rotate), `versions` (metadata) under `ml-platform/` and `agent-platform/` | TTL 720 h, not renewable |
| Admin login (`$KEYDIR/admin-login.json`) | `login --role vault-admin` with a ServiceAccount token from `kubectl create token` | starting and feeding a root-generation ceremony, nothing else | 15 min |
| Temporary root (`$KEYDIR/admin-token.json`) | `generate-root` (admin login + 2 recovery keys) | policy and role changes, a new operator token | `retire-root --token-file` right after use |

**Policy or role change:**

```bash
kubectl --context $CTX -n ewallet-vault create token vault-admin --audience vault --duration 10m \
  | $V login --role vault-admin --token-out $KEYDIR/admin-login.json
$V generate-root --keys-file $KEYDIR/vault-keys.json --token-file $KEYDIR/admin-login.json --token-out $KEYDIR/admin-token.json
$V configure --token-file $KEYDIR/admin-token.json
$V retire-root --token-file $KEYDIR/admin-token.json
rm $KEYDIR/admin-login.json $KEYDIR/admin-token.json
```

`generate-root` behaviour:

- it submits the recovery keys and stops at the threshold;
- one key cannot finish: it cancels its attempt and fails;
- a forged share is rejected by Vault (HTTP 400) when the threshold is reached;
- every API error stops it.

All of this is rehearsed on kind (phase `ceremony`).

**Operator token expired or lost:** run the first two commands above, then
`$V operator-token --token-file $KEYDIR/admin-token.json --token-out $KEYDIR/operator-token-<date>.json`,
then `retire-root --token-file` and the `rm`. Token files are never overwritten.

**Break-glass** (owner approval; only if the admin login cannot work, for example broken Kubernetes
auth): as in V1, a local uncommitted override with `enable_unauthenticated_access = ["generate-root"]`,
the ceremony with the recovery keys through the direct API, repair, then restore the secure
configuration at once. Not exercised.

## 12. GKE sleep and wake

- **Sleep:** the campaign's approved cloud-lab sleep procedure. Nothing Vault-specific is added.
  Vault has no PodDisruptionBudget: the chart's one-replica budget (`maxUnavailable: 0`) would stall
  the resize to zero. **Never delete the `ewallet-vault` namespace or its PVCs while sleeping**.
- **Wake: Vault unseals itself** through Cloud KMS once its pod starts: no unseal command, no recovery
  key. Check `$V status` (`sealed: false`) and `kubectl -n ewallet-vault get pod vault-0` (Ready).
  Then force a sync and check `SecretSynced`. If the operator token expired during the sleep, replace it.
- **Auto-unseal failure** (Cloud KMS unreachable, IAM binding removed, key version disabled):
  - Expected behaviour, observed on kind with the transit seal: `vault-0` never becomes Ready and
    never serves, and after about 110 s it exits with `error parsing Seal configuration` and kubelet
    restarts it.
  - Synced Secrets stay in place, so consumers keep running and even start.
  - Fix the cause (step 0). Kubelet's next start unseals Vault by itself.
  - Recovery keys cannot help here, and `$V unseal` refuses.
  - The exact GKE behaviour is NOT_VERIFIED_ON_GKE.
- **Certificate renewal:** cert-manager renews `vault-server-tls` 15 days before its 90-day expiry.
  A running Vault keeps the certificate it loaded, so restart `vault-0` after a renewal; it
  auto-unseals. When the `vault-ca` CA is renewed, run `scripts/vault/publish_ca.sh $CTX`
  (`CA_UPDATED`).

## 13. Safe cleanup (MUTATING, Kubernetes)

Only on a separate decision; the order matters:

1. Roll the consumers back (step 9).
2. `kubectl --context $CTX delete -f infra/kubernetes/external-secrets/ml-platform.yaml -f infra/kubernetes/external-secrets/ewallet-agents.yaml`.
3. `kubectl --context $CTX delete clustersecretstore ewallet-vault` (by name; never all stores).
4. `helm --kube-context $CTX uninstall external-secrets -n external-secrets`. The chart owns the
   ESO CRDs (`installCRDs: true`). Uninstalling deletes them and every store and ExternalSecret,
   and then every Secret those ExternalSecrets own. Never uninstall ESO while a workload still
   reads an ESO-owned Secret.
5. `helm --kube-context $CTX uninstall vault -n ewallet-vault`. **The PVCs remain.** Deleting them
   destroys all Vault data: its own owner decision, after a Raft snapshot.
6. **The Cloud KMS key stays.** Destroying it is a separate owner decision (step 0).

## Recovery from an incomplete installation

| Where it stopped | State | Recovery |
|---|---|---|
| Step 2: Vault does not start (`error parsing Seal configuration`) | No data yet | Step 0 is incomplete (key, IAM, Workload Identity). Fix it; the pod restarts by itself |
| Step 3: `init` failed with any other error | `$V status` shows `initialized: false`; the empty key file was removed | Retry `init` |
| Step 3: `vault_initialised_keys_not_stored`, `vault_init_outcome_unknown` or `vault_init_shares_unexpected` | Vault may be initialised without stored recovery keys | **Stop; never run `init` again.** Vault holds no secret yet, so the owner may decide to uninstall, delete the PVCs and start again |
| Step 4: `configure` stopped part-way | Some policies or roles applied | Re-run `configure`: every step is idempotent. If root is already retired, use the admin path |
| Step 6: ESO install failed | Partial release | `helm uninstall external-secrets -n external-secrets`, then retry |
| Step 6: `ewallet-vault` not Ready | No sync | Read the cause in `kubectl -n external-secrets logs deploy/external-secrets` (CA, role, audience) |
| Step 7: ExternalSecret not `SecretSynced` | No target Secret; consumers unchanged | Read the cause in the ESO log; fix the store, policy or value; force a sync. Do not switch the consumer |
| Step 8: a consumer not Ready after the switch | Old pods still serve, or the rollout is stuck | Rollback (step 9) |

## Local validation (kind)

The kind campaign is the same driver as the GKE rehearsal, `scripts/vault/gke_rehearsal.sh`, with
`REHEARSAL_CONTEXT=kind-*`. On kind it adds:

- the transit "unsealer" (`SIMULATED_AUTO_UNSEAL`);
- the official store `ewallet-vault` from the committed file;
- the `seal` drill;
- the `agents` phase: the real agentgateway and the real vLLM CPU image with the staged model.

The phase order and the published runs are in [VAULT_GKE_REHEARSAL.md §10](VAULT_GKE_REHEARSAL.md#10-vault-v2-on-kind).
cert-manager v1.16.2 is installed for the run and removed afterwards with
`scripts/vault/rehearsal/kind_cert_manager.sh install|remove kind-llm-agent-local`.
