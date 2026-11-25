# Centralized secret management: HashiCorp Vault and External Secrets Operator (Vault V2)

E-Wallet keeps application credentials in one HashiCorp Vault. The External Secrets Operator
(ESO) copies each credential into the namespace that uses it, as an ordinary Kubernetes Secret,
through **one shared ClusterSecretStore**. Applications keep reading Secrets exactly as before.
Vault is the source of truth, with one read-only reader identity, versioned values, an audit
log and, on GKE, **Cloud KMS auto-unseal**.

Design record: [VAULT_V2_ARCHITECTURE_AUDIT.md](VAULT_V2_ARCHITECTURE_AUDIT.md) (decisions, inventory,
trade-offs). Operating procedure: [VAULT_ESO_RUNBOOK.md](../../runbooks/edai2/VAULT_ESO_RUNBOOK.md).

| | State |
|---|---|
| **CURRENT GKE STATE** | **Nothing of V1 or V2 is deployed.** No Vault, ESO, store or ExternalSecret exists on `ewallet-dev` (Campaign A removed everything; disks proven deleted). No Cloud KMS key exists |
| **EXPERIMENTAL SOURCE IMPLEMENTATION** | Branch `experiment/edai2-vault-clustersecretstore`: ClusterSecretStore `ewallet-vault`, one reader role, the `gcpckms` seal and recovery-key CLI, three credential groups (Feast Redis, vLLM key, gateway client key), opt-in consumer overlays |
| **VERIFIED kind RESULTS** | The full V2 campaign on kind (*Validation*). Auto-unseal there is a local transit seal: **`SIMULATED_AUTO_UNSEAL`**, never a Cloud KMS result |
| **PLANNED GKE DEPLOYMENT** | P4-A done offline: Terraform module, plan guard and read-only KMS preflight ([audit §23](VAULT_V2_ARCHITECTURE_AUDIT.md)). Next: P4-B (create the Cloud KMS key ring, key, GSA, IAM and Workload Identity; owner approval per step), then GKE Campaign A2 (synthetic), then Campaign B (real credentials, owner screenshots) |
| **UNVERIFIED CLOUD BEHAVIOUR** | Cloud KMS, its IAM, Workload Identity for Vault, auto-unseal across a GKE sleep and wake, Vault's behaviour when KMS is unreachable: **NOT_VERIFIED_ON_GKE** |
| `OWNER_EVIDENCE_COMPLETE` (row 54) | **No** |

The V1 design (namespaced SecretStores, Shamir unseal) is described in this file's Git history up to
`f5ebe77b` and stays on branch `feature/edai2-vault-eso`. Its evidence is listed under *History*.

## Why centralize secrets

Without Vault, every credential is a Kubernetes Secret created by a script or by hand in the namespace
that uses it, for example `ml-platform/feast-online-redis`, `ewallet-agents/vllm-api-key` and
`ewallet-agents/agentgateway-client-key`. That works, but:

- no single place says which credentials exist, who may read them and which version is live;
- a rotation means finding and editing every copy;
- Secrets carry no version history and no audit trail.

Vault adds one store with versioned values (KV v2), identity-based access (Kubernetes authentication),
path policies and an audit log. ESO delivers the values without changing the workloads.

## Why this shape

| Choice | Reason |
|---|---|
| HashiCorp Vault, official chart `vault-0.34.1` (Vault 2.0.4, image pinned by digest), one replica | One Vault for the whole platform. Raft on a 5 GiB PVC, audit log on a 1 GiB PVC. HA would triple cost for a lab that sleeps most of the day |
| Internal TLS from a namespace-scoped cert-manager CA | No plaintext listener, no public endpoint; ESO verifies Vault with one published CA |
| **Cloud KMS auto-unseal** (`seal "gcpckms"`), Workload Identity, no JSON key | Every restart, drain or wake unseals by itself. The V1 manual Shamir unseal after every wake was the main operational burden |
| **One ClusterSecretStore** `ewallet-vault`, exact namespace list | One provider configuration, one reader identity, one role. A new consumer needs one ExternalSecret, not five objects |
| One Vault record per underlying credential | The Feature API and the Fraud API share one Feast Redis credential, so they share one record (V1 had two copies) |
| ESO, not the Vault Agent Injector or the CSI driver | Workloads already consume `secretKeyRef`; no sidecar, no webhook in the pod path, no application change |
| MCP credentials stay Kubernetes-only (MCP-A) | Their release-based lifecycle (transition and final server sets) is the rotation mechanism; Vault must not override it (audit §11) |

## Architecture

```text
             Google Cloud KMS  ewallet-vault / vault-unseal (asia-southeast1)      [GKE, P4; kind: transit stand-in]
                         ^  decrypt the root key at every start (Workload Identity: GSA ewallet-vault-unseal)
  ewallet-vault  ┌───────┴──────────────────────────────────────────────────┐
                 │ Vault 2.0.4 x1   KV v2 "ewallet"   Kubernetes auth        │
                 │ Raft PVC + audit PVC   TLS (cert-manager)   ACL policies  │
                 └───────▲──────────────────────────────────────────────────┘
                         │ HTTPS :8200 (NetworkPolicy: the ESO controller only)
  external-secrets       │ ESO v2.14.0. Login: role eso-ewallet-reader as SA eso-vault-reader (audience "vault"),
                         │ CA: ConfigMap external-secrets/vault-ca
                 ClusterSecretStore ewallet-vault   conditions.namespaces = [ml-platform, ewallet-agents]
               ┌─────────┴───────────────┐
  ml-platform  ES feast-online-redis      ewallet-agents  ES vllm-api-key           ES agentgateway-client-key
               → feast-online-redis-vault                 → vllm-api-key-vault      → agentgateway-client-key-vault
               → Feature API, Fraud API                   → vLLM, agentgateway      → agentgateway validator,
                                                            (backend)                 ModelConfig → agents
  excluded: ewallet-sandbox, ewallet-ci, ewallet-ingress, ewallet-mcp (MCP credentials stay Kubernetes-only)
```

Trust boundaries:

- **Vault ↔ ESO:** ESO verifies Vault's certificate against `vault-ca`. Vault verifies the reader's
  ServiceAccount token through TokenReview, and maps it to one role (name, namespace and audience must match).
- **ESO ↔ namespaces:** an ExternalSecret may use `ewallet-vault` only from a listed namespace. ESO writes
  only the target Secret named by the ExternalSecret, in the ExternalSecret's namespace.
- **Application ↔ Secret:** the application sees an ordinary Secret. It holds no Vault token, address or
  CA, and never connects to Vault.

## Secret lifecycle

1. **Create:** an operator writes a value on stdin (`ops/vault/cli.py put --token-file <operator token> --stdin`). It is never an argument, and nothing is printed.
2. **Deliver:** ESO logs in as the reader, reads the mapped key and writes the target Secret. It refreshes every hour, or at once on a `force-sync` annotation.
3. **Consume:** the workload reads the Secret through `secretKeyRef` at start-up.
4. **Rotate:** write a new KV version; ESO updates the Secret; restart the consumers (*Rotation and recovery*).
5. **Revoke:** remove the prefix from the reader policy, or delete the ExternalSecret. A failed sync keeps the last value (`deletionPolicy: Retain`).

## Kubernetes authentication, roles and policies

For each sync, ESO:

1. requests a short-lived token (audience `vault`) for `external-secrets/eso-vault-reader`;
2. logs in to role `eso-ewallet-reader`;
3. receives a 5-minute Vault token with that role's policy only (`token_no_default_policy`);
4. reads, then revokes the token.

| Vault role | Bound identity | Policy | May |
|---|---|---|---|
| `eso-ewallet-reader` | `external-secrets/eso-vault-reader`, audience `vault` | `eso-ewallet-reader` | `read` `ewallet/data/ml-platform/*` and `ewallet/data/agent-platform/*`; `lookup-self`, `revoke-self`. No list, no metadata, no write, no `sys/` |
| `vault-admin` | `ewallet-vault/vault-admin` (owner's admin login) | `root-generation` | only the root-generation ceremony, completed with 2 recovery keys |
| none: orphan operator token | — | `secret-operator` | create and update values, read metadata, under the same two prefixes; never read values |

Source: [bootstrap.json](../../../infra/kubernetes/vault/bootstrap.json), [policies/](../../../infra/kubernetes/vault/policies/).
`eso-vault-reader` is an identity only: no pod runs as it, it has no RBAC and it mounts no token.

Retired in V2:

- the per-namespace readers `eso-ml-platform` and `eso-ewallet-agents`;
- the direct-loader roles `feature-api` and `fraud-api`, which no deployed manifest used.
  Their code (`ai_platform/inference/vault.py`) and opt-in switches remain, but they cannot log in to a V2 Vault.

## External Secrets Operator

[values.yaml](../../../infra/kubernetes/external-secrets/values.yaml) installs the official chart
`external-secrets 2.14.0`, all images pinned by digest, in a restricted-Pod-Security namespace.

- **Processed:** namespaced SecretStores, and ClusterSecretStores
  (`processClusterStore: true`; the rendered controller has no `--enable-cluster-store-reconciler=false`).
- **Off:** ClusterExternalSecret, PushSecret and ClusterPushSecret. Nobody can fan a Secret out to
  every namespace, and ESO never writes to Vault.

### ClusterSecretStore and ExternalSecrets

[cluster-secret-store.yaml](../../../infra/kubernetes/external-secrets/cluster-secret-store.yaml):
`ewallet-vault`, server `https://vault.ewallet-vault.svc.cluster.local:8200`, mount `ewallet`, KV `v2`.
The CA is ConfigMap `vault-ca` in `external-secrets`, published by
[publish_ca.sh](../../../scripts/vault/publish_ca.sh), which never overwrites a ConfigMap it did not
publish. Kubernetes auth uses role `eso-ewallet-reader` and SA `external-secrets/eso-vault-reader`
with audience `vault`.

| ExternalSecret | Vault record | Target (new Secret) | Consumers |
|---|---|---|---|
| `ml-platform/feast-online-redis` | `ml-platform/feast-online-redis#connection_string` | `feast-online-redis-vault` | Feature API, Fraud API |
| `ewallet-agents/vllm-api-key` | `agent-platform/vllm-api-key#api-key` | `vllm-api-key-vault` | vLLM (server), agentgateway (towards vLLM) |
| `ewallet-agents/agentgateway-client-key` | `agent-platform/agentgateway-client-key#api-key` | `agentgateway-client-key-vault` | agentgateway (validator), ModelConfig → agents |

Each uses `creationPolicy: Owner` on a **new** name (deleting the ExternalSecret deletes the target) and
`deletionPolicy: Retain` (a vanished Vault entry keeps the last value). No original Secret is adopted,
changed or deleted, so each stays a clean rollback target.

## Namespace isolation

The trust model of V2 (audit §7.4):

- **The store's condition** decides which namespaces may use `ewallet-vault`: exactly `ml-platform` and
  `ewallet-agents`. It uses names, not labels, because whoever may label a namespace would otherwise join.
  `ewallet-sandbox`, `ewallet-ci`, `ewallet-ingress` and `ewallet-mcp` are excluded.
- **The reader's policy** decides which paths can be read: the two prefixes, nothing else.
- **Kubernetes RBAC** decides who may create ExternalSecrets in the two namespaces. The condition does not
  limit which path an allowed ExternalSecret requests.

**Retired invariant:** V1 proved that the ml-platform reader could not read agent-platform paths. With one
reader for both trusted namespaces this is intentionally gone (decision O1). An ExternalSecret in
`ml-platform` *could* request `agent-platform/vllm-api-key`. What remains is tested on kind (*Validation*):

- an unapproved namespace and the sandbox are refused by the store;
- a path outside the prefixes is refused by Vault;
- a wrong ServiceAccount, a same-named reader in a consumer namespace, a wrong audience and an untrusted CA all fail to log in;
- the reader's own token cannot list, read metadata, write or reach `sys/`;
- a non-ESO pod cannot reach Vault (NetworkPolicy).

## Feature API and Fraud API integration

Both read their Redis connection string through `secretKeyRef`:

- **Feature API:** the opt-in overlay [values-vault-eso.yaml](../../../infra/helm/ewallet-serving/values-vault-eso.yaml)
  changes only the Secret name to `feast-online-redis-vault`.
- **Fraud API:** the strategic-merge patch [fraud-api-eso.patch.yaml](../../../infra/kubernetes/fraud-serving/fraud-api-eso.patch.yaml)
  changes only that one env reference (tested: every other env var and mount unchanged).

No template or Python change. The chart defaults are unchanged: without the overlay, both read
`feast-online-redis`.

Migration order:

1. shadow sync;
2. `shadow_compare.sh` = `SHADOW_MATCH`;
3. switch the Feature API, then a real lookup;
4. switch the Fraud API;
5. rollback is the previous Helm revision, or `rollout undo`.

## Agent Platform integration

The opt-in overlay [values-vault-eso.yaml](../../../infra/helm/llm-agent-platform/values-vault-eso.yaml) moves the
three references in **one** Helm upgrade:

- `vllm.apiKeySecret`: vLLM primary, fallback and candidate, and agentgateway's backend credential;
- `agentgateway.clientKeySecret`: the gateway's `apiKey` validator;
- `modelConfig.apiKeySecret`: ModelConfig `ewallet-global-llm`, and through it every BYO agent's gateway key.

A client and its validator therefore never disagree. On the deployed agent profile (g3 + AGI BYO agents),
the render test proves:

- every gateway-key reference moves;
- the MCP credential releases and the Coordinator idempotency Secret do not move.

The switch is safe only after `SHADOW_MATCH` for both pairs. Rotation needs coordinated restarts:

- the gateway accepts one client key, so agents must restart with it;
- vLLM and the gateway must restart together for a new backend key.

Both behaviours were observed on kind with the real images (*Validation*).

## MCP credentials

Preserved unchanged (MCP-A): `ops/llm_agent/g3_mcp_credentials.py` keeps generating immutable,
release-scoped client Secrets and server tables, and Helm keeps selecting them by `serverSet` and
`clientRelease`. A Vault-backed variant is specified (MCP-B, audit §11): explicit release paths, create-only
writes, `CreatedOnce` + `immutable` + `creationPolicy: Orphan`. It is **not** implemented.

## Auto-unseal and recovery keys

- **GKE configuration** ([values-gke.yaml](../../../infra/kubernetes/vault/values-gke.yaml)):
  - the seal: `seal "gcpckms" { project = "ewallet-data-ai-platform-hg26", region = "asia-southeast1",
    key_ring = "ewallet-vault", crypto_key = "vault-unseal" }`;
  - no credentials file: Application Default Credentials come from Workload Identity, through the KSA
    annotation for GSA `ewallet-vault-unseal`;
  - the KMS resources and IAM are defined in `infra/terraform/gcp/modules/vault_kms` (P4-A) but do not exist yet (P4-B).
- **kind configuration** ([values-local.yaml](../../../infra/kubernetes/vault/values-local.yaml)):
  - identical except the seal: a transit seal on a throwaway "unsealer" Vault
    ([unsealer.py](../../../scripts/vault/rehearsal/unsealer.py));
  - the unsealer's token may only encrypt and decrypt with one key;
  - results there are `SIMULATED_AUTO_UNSEAL`.
- **Initialisation** (`ops/vault/cli.py init`) reads the seal type. Under auto-unseal it creates
  **3 recovery keys, threshold 2**, and stores them with the initial root token in one mode-0600 file
  outside the repository.
  - Recovery keys **cannot unseal** Vault; `unseal` refuses on an auto-unseal Vault and submits nothing.
  - They authorise the root-generation ceremony (with the `vault-admin` login) and rekeying.
- **Readiness:** an uninitialised Vault is Ready (bootstrap); afterwards **Ready means unsealed**.
- **When the seal is unreachable** (KMS down, IAM removed, key version disabled; on kind: the unsealer
  Service without endpoints):
  - Vault never becomes Ready and never serves: its listener never opens;
  - after about 110 s it exits (`error parsing Seal configuration: context deadline exceeded`), and kubelet
    restarts it with back-off;
  - when the seal returns, the next start unseals by itself;
  - synced Secrets stay in place, and consumers keep running and even start.

  This was observed on kind with the transit seal; the GKE behaviour is NOT_VERIFIED_ON_GKE.
- **Key lifecycle:**
  - no scheduled key rotation in V2.0, because each rotation adds a billable version that must stay enabled;
  - the key must never be disabled or destroyed: a destroyed version is unrecoverable, even from backups;
  - rehearsal cleanup never touches KMS.

## Rotation and recovery

- **Rotation:**
  - `put` writes version N+1; older versions stay;
  - ESO updates the target within an hour, or at once with `force-sync`;
  - consumers read at start-up, so a restart completes the rotation.

  A real Redis rotation changes the server password first. Rehearsed on kind: the server password is
  rotated with `CONFIG SET` and the data kept, then Vault, ESO and the consumers follow, and the retired
  credential is refused.
- **Vault restart, drain or wake:** Vault unseals itself, with no manual step; ESO resumes on its next reconcile.
- **Persistence:**
  - Raft data and the audit log are on two Persistent Disks (`data-vault-0`, `audit-vault-0`);
  - `Retain` on both PVs keeps the disks if a PVC is deleted, and the runbook re-binds them (proven on kind);
  - it is not a backup, and a retained disk is unreadable without the KMS key.

### Vault tokens

| Credential | Purpose | Lifetime |
|---|---|---|
| Initial root | applies the bootstrap and creates the operator token, once | revoked right after (`retire-root`) |
| Operator token | writes and rotates values, reads metadata; policy `secret-operator` | orphan, non-renewable, 720 h |
| Admin login | starts and feeds a root-generation ceremony; policy `root-generation` only | 15 min, from `kubectl create token` for `ewallet-vault/vault-admin` |
| Temporary root | later policy or role changes, a new operator token | needs the admin login **and** 2 recovery keys; revoked right after use |
| ESO tokens | one sync each | 5 min, revoked after use |

Routine work never needs root, and root generation stays authenticated (the Vault 2.0 default; no
`enable_unauthenticated_access`).

**Known limitation:** with one owner, the three recovery keys sit in one file on the owner's two offline media.

## Security controls

| Control | Where |
|---|---|
| No public endpoint, `ui: false`, ClusterIP only | `values-gke.yaml`; no HTTPRoute or VirtualService names Vault (test) |
| TLS 1.2+ with a pinned CA; an untrusted CA is refused (tested) | `certificate.yaml`, store `caProvider` |
| NetworkPolicy: port 8200 only from the ESO controller | `network-policy.yaml` |
| One reader, two read-only prefixes, no list, metadata, write or `sys/` (tested live) | `policies/eso-ewallet-reader.hcl` |
| Store limited to an exact namespace list (tested: unapproved namespace, sandbox) | `cluster-secret-store.yaml` |
| Cloud KMS auto-unseal with Workload Identity, no JSON key; recovery keys never unseal | `values-gke.yaml`, `ops/vault/cli.py` |
| Short-lived tokens; root retired; authenticated root generation | role TTLs, `operator-token`, `root-generation.hcl` |
| Audit device on its own PVC; values HMAC-ed | `apply_audit` |
| Restricted Pod Security; images pinned by digest | namespace files, chart values |
| Values only on stdin; key and token files mode 0600 outside the repo; comparisons print a status, never a value | `ops/vault/cli.py`, `scripts/vault/shadow_compare.sh` |

## Operational limitations

- **Single replica:** while Vault is down, no new value is delivered. Synced Secrets keep working.
- **Auto-unseal depends on Cloud KMS:** a disabled key version, a removed IAM binding or a KMS outage keeps
  Vault down at its next start (until fixed). Never destroy the key.
- **The shared reader** can read both prefixes for both trusted namespaces (retired invariant above).
- **Refresh is not instant** (1 h): rotations use `force-sync` and a restart.
- **No PodDisruptionBudget for Vault:** a drain restarts it; it auto-unseals.
- **ESO is trusted to mint tokens** for the reader identity and to write Secrets in every namespace (chart RBAC).
- **Not covered by V2.0:** the Redis server password (`feature-store/redis`), MLflow, Grafana, Jenkins,
  edge basic-auth, the Coordinator idempotency Redis and all MCP credentials (audit §4).
- On GKE, Vault and ESO add about 140m CPU and 384 MiB memory in requests, plus 6 GiB
  of Persistent Disk. The Cloud KMS key costs about US$0.06 a month per active version, also while asleep.

## Validation and evidence

All V2 results are **kind** results (`ENVIRONMENT=kind`) with **simulated** auto-unseal
(`AUTO_UNSEAL=SIMULATED_AUTO_UNSEAL`). None is a GKE or Cloud KMS result. The driver is
[`gke_rehearsal.sh`](../../../scripts/vault/gke_rehearsal.sh) in kind mode; the phase order is in
[VAULT_GKE_REHEARSAL.md §10](../../runbooks/edai2/VAULT_GKE_REHEARSAL.md#10-vault-v2-on-kind).

| Run | Result |
|---|---|
| [`rehearsal_v2_kind_vault-rehearsal-20261125T074917Z`](../../evidence/edai2/vault/rehearsal_v2_kind_vault-rehearsal-20261125T074917Z/README.md) | **129 records, 0 FAIL, all gates open; kind restored to its baseline.** K1–K16 of the audit's test matrix |
| [`rehearsal_v2_faults_kind_20261125`](../../evidence/edai2/vault/rehearsal_v2_faults_kind_20261125/README.md) | Five interrupted `Retain` drills, one interrupted cleanup: every stopped run refused its normal phases and cleaned up from its exact volume state |

| Area | Result (kind) |
|---|---|
| Auto-unseal (simulated) | 3/2 **recovery keys**; Vault unsealed itself after init, after every restart, after the Released-PV recovery and after the simulated wake; manual `unseal` refused |
| Seal unreachable (simulated) | Vault never Ready, API never up, exited on seal setup; Secrets kept; the Feature API kept serving and even started; Vault recovered by itself when the seal came back |
| Recovery-key ceremony | 1 key insufficient; a forged share rejected (400); 2 keys → root, used, retired (403) |
| ClusterSecretStore | The committed `ewallet-vault` and the rehearsal store `True Valid`; one reader across two namespaces; three ExternalSecrets synced and owned |
| Feature API (real image and chart) | 9 lookups `BASELINE_OK` (HTTP 200, 31 features): baseline, switch, rollback, re-upgrade, after agents, after rotation, while Vault down, after PV recovery, after wake |
| Agent Gateway + vLLM (real images and chart) | 200 with the client key from Vault; 401 wrong, missing, or with the backend key; the overlay moved validator, backend and ModelConfig together; client-key rotation needs a gateway restart; a half-rotated backend key fails, and vLLM refuses the other key directly (200/401); Helm rollback and re-upgrade 200 |
| Negative | out-of-policy path, missing key, wrong ServiceAccount, same-named reader in a consumer namespace, wrong audience, untrusted CA, unapproved namespace, sandbox: each with its expected cause and no target Secret. The reader's own token got 200 on a read inside its prefix and 403 for everything else tried, including root generation. NetworkPolicy blocked a non-ESO pod |
| Rotation | Redis: server password rotated (`CONFIG SET`, data kept) → Vault → ESO → consumers; the retired credential refused |
| Persistence and cleanup | `Retain` drill, recovery, simulated wake; cleanup by recorded identity only; kind back to its original state |

Static and contract tests: [`test_vault_eso.py`](../../../tests/ops/test_vault_eso.py) (store, policy, CRD schemas,
rendered controller flags, overlays, MCP and idempotency Secrets untouched), `test_vault_tools.py` (recovery keys,
seal configuration, unsealer), `test_vault_rehearsal.py` (driver safety, stub cleanup), `test_r2_serving_identity.py`.

### History (V1, branch `feature/edai2-vault-eso`)

| Run | Environment | Result |
|---|---|---|
| [`eso_local_20261124T055021Z`](../../evidence/edai2/vault/eso_local_20261124T055021Z/results.jsonl) | kind | 65/65, the V1 namespaced chain |
| [`gke_rehearsal_vault-rehearsal-20261124T173156Z`](../../evidence/edai2/vault/gke_rehearsal_vault-rehearsal-20261124T173156Z/README.md) | **GKE** | Campaign A complete (V1: Shamir, namespaced store); everything removed |
| [`rehearsal_aplus_kind_20261124T191350Z`](../../evidence/edai2/vault/rehearsal_aplus_kind_20261124T191350Z/README.md) | kind | A+: real Feature API, Helm switch and rollback, `Retain`, simulated wake |
| [`rehearsal_aplus_faults_kind_20261125`](../../evidence/edai2/vault/rehearsal_aplus_faults_kind_20261125/README.md) | kind | fail-fast and cleanup from five interrupted points of the `Retain` drill |

The V1 validators (`eso_local_validate.sh`, `local_validate.sh`) were removed in V2; they remain in Git history.

### Owner evidence on GKE (after the approved campaigns)

Screenshots illustrate the deployed system; the owner takes them during the official campaign, and none is
generated or reconstructed. The V2 list is in the audit (§16: E1–E9 updated, K-E1–K-E7 for Cloud KMS, the
ClusterSecretStore and Agent Gateway).
