# Centralized secret management: HashiCorp Vault and External Secrets Operator

E-Wallet keeps application credentials in one HashiCorp Vault. The External Secrets Operator
(ESO) copies each credential into the namespace that uses it, as an ordinary Kubernetes
Secret. Applications keep reading Secrets exactly as before; Vault becomes the source of
truth, with per-namespace identities, least-privilege policies, versioning and an audit log.

Operating procedure: [VAULT_ESO_RUNBOOK.md](../../runbooks/edai2/VAULT_ESO_RUNBOOK.md).

| Status | State |
|---|---|
| `IMPLEMENTED` (in Git) | Yes: Vault chart values, bootstrap (policies, roles), token lifecycle, ESO installation, SecretStores, ExternalSecrets, Feature API overlay |
| `LOCAL_VALIDATED` (on kind) | Yes: full chain with the real charts and the real Feature API (*Validation*) |
| `GKE_DEPLOYED` | **No**: no Vault, ESO, SecretStore or ExternalSecret exists on GKE (read-only preflight 2026-11-24: `VAULT_GKE_PREFLIGHT_READY`; campaign plan in [VAULT_GKE_ACCEPTANCE_CAMPAIGN.md](../../runbooks/edai2/VAULT_GKE_ACCEPTANCE_CAMPAIGN.md)) |
| `GKE_RUNTIME_VERIFIED` | No |
| `OWNER_EVIDENCE_COMPLETE` (row 54) | **No**: not yet accepted as row 54 evidence; that needs the approved GKE campaign and the owner's evidence |

## Why centralize secrets

Without Vault, the manifests and charts in this repository expect every credential as a
Kubernetes Secret, created by a script or by hand in the namespace that uses it: for example
`ml-platform/feast-online-redis`, `ewallet-agents/agentgateway-client-key` and
`ewallet-agents/vllm-api-key`. That works, but:

- there is no single place that says which credentials exist, who may read them and which version is live;
- a rotation means finding and editing every copy by hand;
- Secrets carry no version history and no audit trail of who read them.

Vault provides one store with versioned values (KV v2), identity-based access (Kubernetes
authentication), per-path policies and an audit log. ESO delivers those values to workloads
without changing the workloads.

## Why Vault, and why this shape

| Choice | Reason |
|---|---|
| HashiCorp Vault, official chart `vault-0.34.1` (Vault 2.0.4, image pinned by digest) | The industry-standard secret store, runnable inside the cluster at no licence cost; not dev mode |
| One server, integrated Raft on a 5 GiB PVC, audit log on a 1 GiB PVC | Vault is stateful and its data survives restarts. Three-node HA would triple cost for a cluster that sleeps most of the day |
| Internal TLS from a namespace-scoped cert-manager CA | Clients verify Vault with one published CA (`vault-ca`); no plaintext listener, no public endpoint |
| Manual Shamir unseal (3 shares, threshold 2) | Cloud KMS auto-unseal would add a billable resource, new IAM and a cloud dependency |
| ESO for delivery, not the Vault Agent Injector or CSI driver | Workloads already consume `secretKeyRef`; ESO needs no sidecar, no webhook in the pod path and no application change |
| Namespaced `SecretStore`s, not one `ClusterSecretStore` | Each namespace logs in with its own identity, so one namespace's store cannot read another namespace's paths (*Namespace isolation*) |

The reference project (RecSys-MLops) uses Vault HA with KMS auto-unseal and one
`ClusterSecretStore` whose single ESO identity can read every path. E-Wallet keeps the same
delivery idea (Vault → ESO → namespace-local Secret) but stays smaller (one Vault, manual
unseal) and narrows access per namespace.

## Architecture

```text
                 ewallet-vault namespace (restricted Pod Security, no public route)
              ┌────────────────────────────────────────────────────────┐
              │ Vault 2.0.4, 1 replica      KV v2 mount "ewallet"       │
              │ Raft PVC + audit PVC        Kubernetes auth "kubernetes"│
              │ TLS (cert-manager CA)       ACL policies, Shamir seal   │
              └───────────────▲────────────────────────────────────────┘
                              │ HTTPS :8200, NetworkPolicy: the ESO controller
                              │ (plus the two direct-loader serving pods)
              ┌───────────────┴───────────────┐
              │ external-secrets namespace     │ ESO v2.14.0: controller, webhook,
              │ one ESO installation           │ cert-controller (only the controller calls Vault)
              └───────┬────────────────┬──────┘
  login as ml-platform/│                │ login as ewallet-agents/
  eso-vault-reader     │                │ eso-vault-reader (staged)
  (audience "vault")   ▼                ▼
  ml-platform: SecretStore vault         ewallet-agents: SecretStore vault
    ExternalSecret feature-api-redis       ExternalSecret agentgateway-client-key-vault
    → Secret feature-api-redis-vault       → Secret agentgateway-client-key-vault (no consumer yet)
    → Feature API (secretKeyRef) → Redis
```

Trust boundaries:

- **Vault ↔ ESO:** ESO verifies Vault's certificate against `vault-ca`. Vault verifies ESO's
  ServiceAccount token through the Kubernetes TokenReview API and maps it to one role.
- **ESO ↔ namespace:** ESO writes only the Secret named by an ExternalSecret in the same
  namespace as its SecretStore.
- **Application ↔ Secret:** the application sees an ordinary Secret. It holds no Vault token,
  no Vault address and no CA, and never connects to Vault.

## Secret lifecycle

1. **Create:** an operator writes a value into Vault on stdin (`ops/vault/cli.py put --token-file <operator token> --stdin`).
   It is never a command-line argument, and nothing is printed.
2. **Deliver:** ESO logs in with the namespace's reader identity, reads the mapped key and
   writes the target Secret. It refreshes every hour (`refreshInterval: 1h`), or at once on a
   `force-sync` annotation.
3. **Consume:** the workload reads the Secret through `secretKeyRef` at start-up.
4. **Rotate:** write a new KV version; ESO updates the Secret; restart the workload (*Rotation and recovery*).
5. **Revoke:** remove the path from the namespace policy, or delete the role. ESO's next sync
   fails and the Secret keeps its last value until removed.

## Kubernetes authentication, roles and policies

ESO never holds a long-lived Vault token. For each sync it:

1. requests a short-lived token (audience `vault`) for the namespace's `eso-vault-reader` ServiceAccount;
2. logs in to Vault role `eso-<namespace>`. Vault checks the token through TokenReview (Vault's
   ServiceAccount has `system:auth-delegator`), and checks that the account name, namespace and
   audience match the role;
3. receives a Vault token with a 5-minute TTL and only that role's policy (`token_no_default_policy`);
4. reads the mapped path, then revokes the token.

| Vault role | Bound identity | Policy | Readable |
|---|---|---|---|
| `eso-ml-platform` | `ml-platform/eso-vault-reader` | `eso-ml-platform` | `ewallet/data/feature-api/feast-redis` |
| `eso-ewallet-agents` | `ewallet-agents/eso-vault-reader` | `eso-ewallet-agents` | `ewallet/data/agent-platform/agentgateway-client-key` |
| `feature-api`, `fraud-api` | the serving accounts (direct loader) | same names | each its own `…/feast-redis` |
| none: orphan operator token (*Vault tokens*) | — | `secret-operator` | writes values and reads metadata, never reads values |
| `vault-admin` | `ewallet-vault/vault-admin` (owner's admin login) | `root-generation` | nothing; may only run the root-generation ceremony |

Each reader policy grants `read` on exactly the paths its namespace's ExternalSecrets map,
plus `lookup-self` and `revoke-self` for its own token. There is no `list` and no metadata
access, so a reader cannot discover other paths. `eso-vault-reader` is an identity only:
no pod runs as it, it has no RBAC and it mounts no token. Source:
[bootstrap.json](../../../infra/kubernetes/vault/bootstrap.json) and
[policies/](../../../infra/kubernetes/vault/policies/).

## External Secrets Operator

[values.yaml](../../../infra/kubernetes/external-secrets/values.yaml) installs the official
chart `external-secrets 2.14.0`. All three images are pinned by digest, and the namespace has
restricted Pod Security. ESO processes only namespaced `SecretStore`s:
`ClusterSecretStore`, `ClusterExternalSecret` and `PushSecret` processing is switched off,
so nobody can create a store that bypasses the per-namespace identities, and ESO never writes
back to Vault. The controller's chart RBAC lets it read and write Secrets, and create tokens
for ServiceAccounts, which is how it obtains each namespace's reader token.

### SecretStore and ExternalSecret

[ml-platform.yaml](../../../infra/kubernetes/external-secrets/ml-platform.yaml):

- `SecretStore vault`: `https://vault.ewallet-vault.svc.cluster.local:8200`, mount `ewallet`,
  KV `v2`, CA from ConfigMap `vault-ca`, Kubernetes auth with role `eso-ml-platform` and
  ServiceAccount `eso-vault-reader`, audience `vault`.
- `ExternalSecret feature-api-redis`: one key, `connection_string` from
  `feature-api/feast-redis`, into the **new** Secret `feature-api-redis-vault`.
  `creationPolicy: Owner`: ESO owns that Secret, and deleting the ExternalSecret deletes it.
  `deletionPolicy: Retain`: if the Vault entry disappears, the Secret keeps its last value.

The existing `feast-online-redis` is not adopted, changed or deleted, so it stays a clean rollback target.

## Namespace isolation

Every namespace that receives secrets has its own reader ServiceAccount, Vault role and
policy. A store in `ewallet-agents` cannot log in as `eso-ml-platform` (the role is bound to
the `ml-platform` namespace). Neither reader can read the other's paths, nor the Fraud API's.
The local run tests each of these denials (*Validation*). Adding a namespace means adding
one reader policy, one role in `bootstrap.json` and one manifest like `ml-platform.yaml`.

One shared `ClusterSecretStore`, as in the reference project, would be simpler, but its single
identity could read every path. With the namespaced stores, a mistake in one namespace's
ExternalSecret cannot reach another namespace's secrets.

## Feature API integration

The Feature API already reads its Redis connection string from `secretKeyRef`
(`featureApi.redisSecret`). The opt-in overlay
[values-vault-eso.yaml](../../../infra/helm/ewallet-serving/values-vault-eso.yaml) changes only
the Secret name to `feature-api-redis-vault`, and keeps `featureApi.vault.enabled=false`. No
template or Python change is needed, and the pod gets no Vault token, address or CA. The chart
defaults are unchanged: without the overlay the Feature API reads `feast-online-redis`.

Migration order: (1) shadow sync, Feature API unchanged; (2) read-only check that the new
Secret equals the old one (`scripts/vault/shadow_compare.sh`: only `SHADOW_MATCH` passes; a missing, empty or unreadable value fails); (3) switch with the overlay; (4) real lookup; (5) rollback is the
previous Helm revision. The commands are in the runbook, steps 7–9.

### Existing direct loader

The earlier integration, [`ai_platform/inference/vault.py`](../../../ai_platform/inference/vault.py),
lets a pod log in to Vault itself with its own projected token and load the value into its
environment at start-up (`featureApi.vault.enabled`, Fraud API patch
`fraud-api-vault.patch.yaml`). It is kept and still tested, but it is **superseded by ESO for
new consumers**:

- With the direct loader, every consumer needs Vault code, a CA mount and a Vault role, and it
  cannot start while Vault is sealed.
- With ESO, consumers need nothing new, and a sealed Vault only pauses synchronisation.

The direct-loader roles stay in `bootstrap.json` so the existing Fraud API package keeps
working, but nothing deployed uses them.

## Agent Platform extension

[ewallet-agents.yaml](../../../infra/kubernetes/external-secrets/ewallet-agents.yaml) is
**staged and opt-in**. It has its own store, reader identity and policy, and syncs one
representative credential path (`agent-platform/agentgateway-client-key`) into a shadow
Secret, `agentgateway-client-key-vault`, that no workload reads.

The Agent Platform's current credentials are deliberately not migrated:

| Credential | Owner and lifecycle | Why not now |
|---|---|---|
| `agentgateway-client-key` | Helm values, read by agentgateway and every agent | Clients and gateway must switch together |
| `vllm-api-key` | read by vLLM and agentgateway | Same coordinated switch |
| `mcp-token-*`, `mcp-auth-*-<release>` | versioned, immutable credential releases with their own rotation | Their release IDs are the rotation mechanism; ESO must not override them |
| ModelConfig `apiKeySecret` | kagent CR reference | Changes agent start-up |

Moving one of them is a separate, approved change: seed Vault, shadow-sync, switch every
consumer in one release, keep the old Secret for rollback. For the MCP releases, ESO would
need one immutable ExternalSecret per release version, as in the reference project.

## Rotation and recovery

- **Rotation:** `put` writes version N+1, and older versions stay readable by an operator.
  ESO updates the Secret within an hour, or at once with `force-sync`. The workload reads the
  value at start-up, so a rollout restart completes the rotation. Changing the actual Redis
  password is a separate coordinated step (Redis, every client Secret, restarts).
- **Vault restart or cluster wake:** Vault comes back sealed. ESO syncs fail (the
  ExternalSecret reports `Ready False`), but the synced Secrets remain, so the Feature API
  restarts and serves normally. **A retained Secret is not proof that ESO is syncing**:
  always check the ExternalSecret condition. After a manual unseal, the next sync succeeds.
- **Persistence:** Raft data lives on the PVC. A restart or the node pool scaling to 0 loses
  nothing. Deleting the namespace or the PVCs destroys all secrets.

### Vault tokens

Nobody keeps using the initial root token, and root generation stays authenticated. That is the
Vault 2.0 default, introduced "to prevent attackers from sending key-update requests with bogus
key fragments" (HashiCorp, *Important changes*). The server config has no `enable_unauthenticated_access`.

| Credential | Purpose | Lifetime |
|---|---|---|
| Initial root | applies the bootstrap and creates the operator token, once | revoked right after (`retire-root`) |
| Operator token (`operator-token`) | writes and rotates values, reads metadata; policy `secret-operator` only | orphan, non-renewable, 720 h; it survives the root token's revocation |
| Admin login (`login --role vault-admin`) | starts and feeds a root-generation ceremony; policy `root-generation` only | 15 min; obtained from a `kubectl create token` for `ewallet-vault/vault-admin` |
| Temporary root (`generate-root`) | later policy or role changes and a new operator token | needs the admin login **and** 2 unseal shares; revoked right after use |
| ESO tokens | one sync each | 5 min, revoked after use |

So routine work never needs root. Administration needs two things: the owner's cluster-admin
access (to obtain the admin login) and two unseal shares. Neither of them expires, so an
expired operator token or a long GKE sleep never locks the owner out. ESO readers and
applications have no `sys/` capability.

If Vault's Kubernetes auth itself breaks, no admin login is possible. The runbook then
describes an owner-approved break-glass step: temporarily allow unauthenticated root
generation, repair, and restore the secure configuration at once.

Token files are mode 0600, outside the repository, never overwritten, and never printed. The
local run checks every step:
- root generation without a token is rejected (403), and so is the operator token;
- an ESO reader cannot log in as `vault-admin`, and the admin login can do nothing but root generation;
- one share cannot finish a ceremony; the authenticated ceremony with two shares succeeds;
- the generated root re-applies the bootstrap and issues a replacement operator token, then is retired and rejected;
- a short-lived operator token expires and is rejected.

### Shamir unseal and recovery material

`ops/vault/cli.py init` writes all three unseal shares and the initial root token to one
mode-0600 file outside the repository (it refuses a path inside it, and never overwrites a
file). Unsealing needs two of the three shares. That file is recovery material:

- keep it on two offline encrypted media, never in Git, cloud sync or evidence;
- the root token inside it is retired after bootstrap (*Vault tokens*); `generate-root` makes a temporary one from the shares;
- losing the shares loses the data; there is no other recovery path.

**Known limitation:** with a single owner, all shares sit in one file. Shamir then protects
against losing the cluster, not against someone obtaining that file. Distributing shares to
separate holders would fix this; it is a people decision, not a code change. The seal type
is never migrated: Shamir stays, and KMS auto-unseal is out of scope.

## Security controls

| Control | Where |
|---|---|
| Root generation is authenticated (admin login, policy `root-generation`) and needs two unseal shares; no endpoint is opened without a token | `values-gke.yaml`, `policies/root-generation.hcl`, role `vault-admin` |
| No public endpoint; `ui: false`; ClusterIP only | `values-gke.yaml`; no HTTPRoute or VirtualService names Vault (test) |
| TLS 1.2+ with a pinned CA; ESO verifies it; an untrusted CA is refused (tested) | `certificate.yaml`, SecretStore `caProvider` |
| NetworkPolicy: port 8200 only from the ESO controller and the two serving pods | `network-policy.yaml` |
| Per-namespace identity, role and policy; no wildcards, no `list` | `bootstrap.json`, `policies/eso-*.hcl` |
| ESO uses short-lived tokens (5 min), revoked after use, and no static token. The only long-lived token is the human operator's, scoped to `secret-operator` and expiring after 720 h | role TTLs, ESO Kubernetes auth, `operator-token` |
| Audit device on its own PVC; values are HMAC-ed, not logged | `apply_audit`; the local run counts 0 plaintext matches |
| Restricted Pod Security for Vault and ESO; images pinned by digest | `namespace.yaml` files, chart values |
| Values only on stdin; key and token files mode 0600 outside the repo; the shadow comparison prints a status, never a value | `ops/vault/cli.py`, `scripts/vault/shadow_compare.sh` |

## Operational limitations

- **Single replica:** while Vault is down or sealed, no new secret can be delivered. Existing
  Secrets keep working.
- **Manual unseal after every Vault restart**, including each GKE wake. Until then,
  ExternalSecrets report `Ready False`.
- **Pod Ready does not mean unsealed.** The readiness probe accepts a sealed or uninitialised
  Vault (otherwise `init` could never reach it), so the GKE Workloads page shows `vault-0` Ready
  while it is sealed. Only `ops/vault/cli.py status` (`sealed: false`) proves Vault is usable.
- **An initialisation whose answer is lost cannot be recovered.** `init` creates the key file
  before asking Vault and forces the shares to disk, but if Vault initialises and the answer never
  arrives, no client can obtain the shares. The CLI reports this as
  `vault_initialised_keys_not_stored`; the only way forward is a fresh Vault, before any secret is stored.
- **Refresh is not instant:** one hour by default, so rotations use `force-sync` and a rollout restart.
- **No PodDisruptionBudget for Vault.** With one replica, the chart's budget (`maxUnavailable: 0`) could only
  block node drains, including the cloud-lab resize to zero. A drain or node upgrade therefore restarts
  Vault sealed, and it needs a manual unseal.
- **Owner-held recovery material**, as described above.
- **ESO is trusted to mint tokens.** Its chart RBAC lets the controller create ServiceAccount
  tokens and write Secrets in every namespace. A namespaced store can only use a
  ServiceAccount in its own namespace, so the isolation above also relies on Kubernetes RBAC:
  only platform operators may create `SecretStore` and `ExternalSecret` objects.
- Vault, the ESO pods and the PVCs add about 140m CPU and 450 MiB memory in requests, plus
  6 GiB of Persistent Disk.

## Validation and evidence

Local end-to-end validation: [`scripts/vault/eso_local_validate.sh`](../../../scripts/vault/eso_local_validate.sh)
on kind. It uses the official Vault and ESO charts with the repository values, the repository
bootstrap, NetworkPolicy and manifests, a password-protected Redis, and the real Feature API
image rendered from the serving chart with the overlay. All values are synthetic, and only
SHA-256 prefixes are recorded.

Run [`eso_local_20261124T055021Z`](../../evidence/edai2/vault/eso_local_20261124T055021Z/results.jsonl)
(kind `kind-llm-agent-local`, Vault 2.0.4, ESO v2.14.0, TLS issued by cert-manager v1.16.2 from the
repository's `certificate.yaml`, as on GKE): **65 of 65 steps passed**. Afterwards, the kind cluster
was identical to a snapshot taken before the run: `ewallet-agents` objects and their
resourceVersions, namespaces, CRDs, webhooks and cluster RBAC. The temporary cert-manager was
removed afterwards.

| Area | Result |
|---|---|
| Vault | cert-manager issued the CA and server certificate (chain verified, SANs include `vault.ewallet-vault.svc.cluster.local`); no PodDisruptionBudget; uninitialised → `init` (3/2) → unsealed (`initialized true, sealed false`); bootstrap applied; HTTPS verifies with the CA; an untrusted CA is refused |
| Token lifecycle | Orphan `secret-operator` token created; initial root retired and then rejected; the operator token cannot change a role; all seeding and rotation used the operator token |
| Root generation (authenticated) | Without a token: HTTP 403; with the operator token: denied; an ESO reader cannot log in as `vault-admin`; the admin login can neither configure nor read metadata; 1 share cannot finish (attempt cancelled); admin login plus 2 shares produced a root that re-applied the bootstrap and issued a replacement operator token, then was retired and rejected |
| Operator-token expiry | A 20 s operator token was rejected after expiry; the replacement and the original operator token kept working |
| ESO | 3 deployments Ready; SecretStore `Valid`; ExternalSecret `SecretSynced`; target holds only `connection_string`, owned by the ExternalSecret, hash equal to Vault; `feast-online-redis` unchanged |
| Shadow gate | `shadow_compare.sh`: `SHADOW_MATCH` for the equal pair, `SHADOW_SHADOW_SECRET_MISSING` for an absent Secret, `SHADOW_MISMATCH` after rotation (the old Secret was not rotated) |
| Feature API | Real image from the chart with the overlay (no `EWALLET_VAULT_*`); Feast materialized through the synced Secret; readiness and a real lookup: HTTP 200, 3 features |
| Rollback | Switch to `feast-online-redis` → HTTP 200; switch back → HTTP 200 |
| Second namespace | Before seeding, the `ewallet-agents` ExternalSecret failed with *secret missing*; after seeding, the shadow Secret synced (no consumer) |
| Isolation (expected reason asserted, no target Secret) | ml-platform → Fraud API path and agent path: `permission denied`; ewallet-agents → Feature API path: `permission denied`; ewallet-agents store using the ml-platform role: `namespace not authorized` |
| Other negative cases (expected reason asserted) | Application ServiceAccount: `service account name not authorized`; audience `kubernetes`: `invalid audience`; untrusted CA: `x509`; wrong auth mount: login denied; missing property: key missing; invalid KV version: rejected at admission; consumer with a missing Secret: `CreateContainerConfigError` |
| NetworkPolicy | A listed pod reaches Vault; an unlisted pod in the same namespace cannot |
| Rotation | KV version 2 → ESO synced the new value → rollout restart → lookup HTTP 200 on the rotated (synthetic) Redis password |
| Vault unavailable | Scaled to 0: ExternalSecret `Ready False` with cause *unreachable*; Secret retained; the Feature API restarted on it and served HTTP 200 |
| Seal and persistence | Restart → sealed; sync fails with cause *sealed*; manual unseal → version 2 and the operator token still present from the PVC → sync resumed |
| Audit | 528 lines, 132 Kubernetes logins, 0 plaintext matches of any synthetic value |

A follow-up run, [`eso_local_20261124T061029Z`](../../evidence/edai2/vault/eso_local_20261124T061029Z/results.jsonl)
(64/64, OpenSSL CA), checks the retired root tokens with an idempotent `configure` that must fail with
HTTP 403. The earlier probe, a metadata read of a path not yet written, could not tell a revoked token from
a missing path.

Each negative step passes only if ESO reported the expected cause. The cause comes from the
controller log, because ESO's events are generic. An unreachable Vault, or any other unrelated
failure, therefore fails the step instead of passing as an authorization test. Earlier runs are
kept for history: `eso_local_20261123T210316Z` (42 steps, before the token lifecycle and these
assertions), and `eso_local_20261124T044834Z` (53 steps, which still used the since-removed
unauthenticated root generation), and `eso_local_20261124T051306Z` (63 steps, with an OpenSSL CA instead of cert-manager).

Local means local: kind's CNI (kindnet) enforced the NetworkPolicy, and the Redis was a
disposable in-namespace instance rather than `feature-store/redis`. None of this is a GKE result.

The GKE technical rehearsal ([VAULT_GKE_REHEARSAL.md](../../runbooks/edai2/VAULT_GKE_REHEARSAL.md))
was dry-run end to end on kind with its own driver, [`gke_rehearsal.sh`](../../../scripts/vault/gke_rehearsal.sh):
[`rehearsal_kind_20261124T172656Z`](../../evidence/edai2/vault/rehearsal_kind_20261124T172656Z/README.md),
10 phases and 45 steps with no failure, from preflight to a verified clean cluster. It also
recorded two Vault 2.0.4 defaults seen with the new `describe` command: an `agent-registry/`
mount and a `default-ceiling` policy, neither used here.

The same rehearsal then ran on GKE `ewallet-dev` with synthetic values only
([`gke_rehearsal_vault-rehearsal-20261124T173156Z`](../../evidence/edai2/vault/gke_rehearsal_vault-rehearsal-20261124T173156Z/README.md)): Vault on Persistent Disks with cert-manager TLS,
Kubernetes auth through GKE's TokenReview, ESO sync, a consumer authenticating with the synced
Secret, six denials with their expected causes, Calico enforcing the Vault NetworkPolicy, versioning
and a sealed restart with manual unseal. Everything was then removed (disks proven deleted), the
production configuration was unchanged, and the cluster went back to 0 nodes. It is a rehearsal,
not the owner's row 54 evidence.

Static contract tests: [`tests/ops/test_vault_eso.py`](../../../tests/ops/test_vault_eso.py).
The earlier direct-loader runs remain in `docs/evidence/edai2/vault/local_20261106T183340Z` and `local_r2_20261107T101251Z`.

### Owner evidence on GKE (after an approved campaign)

Screenshots illustrate the deployed system; the owner takes them during the official campaign,
and none is generated or reconstructed. The list, the Console navigation, the PASS criteria, what
must never be visible, and the capture checkpoints are in the acceptance plan's
[visual evidence playbook](../../runbooks/edai2/VAULT_GKE_ACCEPTANCE_CAMPAIGN.md#7-visual-evidence-playbook-owner-screenshots)
(E1–E9). Accepted figures are added to the sections they illustrate, with captions.
