# Centralized secret management: HashiCorp Vault and External Secrets Operator

E-Wallet keeps application credentials in one HashiCorp Vault. The External Secrets Operator
(ESO) copies each credential into the namespace that uses it, as an ordinary Kubernetes
Secret. Applications keep reading Secrets exactly as before; Vault becomes the source of
truth, with per-namespace identities, least-privilege policies, versioning and an audit log.

Operating procedure: [VAULT_ESO_RUNBOOK.md](../../runbooks/edai2/VAULT_ESO_RUNBOOK.md).

| Status | State |
|---|---|
| `IMPLEMENTED` | Yes: Vault chart values, bootstrap (policies, roles), ESO installation, SecretStores, ExternalSecrets, Feature API overlay |
| `LOCAL_VALIDATED` | Yes: full chain on kind with the real charts and the real Feature API (*Validation*) |
| `GKE_DEPLOYED` | No |
| `GKE_RUNTIME_VERIFIED` | No |
| `OWNER_EVIDENCE_COMPLETE` | No |

## Why centralize secrets

Before this work, every credential was a Kubernetes Secret created by a script or by hand in
the namespace that used it, for example `ml-platform/feast-online-redis`,
`ewallet-agents/agentgateway-client-key` and `ewallet-agents/vllm-api-key`. That works, but:

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

1. **Create:** an operator writes a value into Vault on stdin (`ops/vault/cli.py put --stdin`).
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
| none (operator token) | — | `secret-operator` | writes values and reads metadata, never reads values |

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
Secret equals the old one; (3) switch with the overlay; (4) real lookup; (5) rollback is the
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

The live Agent Platform credentials are deliberately not migrated:

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

### Shamir unseal and recovery material

`ops/vault/cli.py init` writes all three unseal shares and the initial root token to one
mode-0600 file outside the repository (it refuses a path inside it, and never overwrites a
file). Unsealing needs two of the three shares. That file is recovery material:

- keep it on two offline encrypted media, never in Git, cloud sync or evidence;
- retire the root token after bootstrap (`retire-root`); a new one can be generated from the shares if needed;
- losing the shares loses the data; there is no other recovery path.

**Known limitation:** with a single owner, all shares sit in one file. Shamir then protects
against losing the cluster, not against someone obtaining that file. Distributing shares to
separate holders would fix this; it is a people decision, not a code change. The seal type
is never migrated: Shamir stays, and KMS auto-unseal is out of scope.

## Security controls

| Control | Where |
|---|---|
| No public endpoint; `ui: false`; ClusterIP only | `values-gke.yaml`; no HTTPRoute or VirtualService names Vault (test) |
| TLS 1.2+ with a pinned CA; ESO verifies it; an untrusted CA is refused (tested) | `certificate.yaml`, SecretStore `caProvider` |
| NetworkPolicy: port 8200 only from the ESO controller and the two serving pods | `network-policy.yaml` |
| Per-namespace identity, role and policy; no wildcards, no `list` | `bootstrap.json`, `policies/eso-*.hcl` |
| Short-lived tokens (5 min) for ESO, revoked after use; no static token anywhere | role TTLs, ESO Kubernetes auth |
| Audit device on its own PVC; values are HMAC-ed, not logged | `apply_audit`; the local run counts 0 plaintext matches |
| Restricted Pod Security for Vault and ESO; images pinned by digest | `namespace.yaml` files, chart values |
| Values only on stdin; key file mode 0600 outside the repo | `ops/vault/cli.py` |

## Operational limitations

- **Single replica:** while Vault is down or sealed, no new secret can be delivered. Existing
  Secrets keep working.
- **Manual unseal after every Vault restart**, including each GKE wake. Until then,
  ExternalSecrets report `Ready False`.
- **Refresh is not instant:** one hour by default, so rotations use `force-sync` and a rollout restart.
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

Run [`eso_local_20261123T210316Z`](../../evidence/edai2/vault/eso_local_20261123T210316Z/results.jsonl)
(kind `kind-llm-agent-local`, Vault 2.0.4, ESO v2.14.0): **42 of 42 steps passed**, then everything it created was removed.

| Area | Result |
|---|---|
| Vault | Uninitialised → `init` (3/2) → unsealed (`initialized true, sealed false`); bootstrap applied; HTTPS verifies with the CA; an untrusted CA is refused |
| ESO | 3 deployments Ready; SecretStore `Valid`; ExternalSecret `SecretSynced`; target holds only `connection_string`, owned by the ExternalSecret, hash equal to Vault; `feast-online-redis` unchanged |
| Feature API | Real image from the chart with the overlay (no `EWALLET_VAULT_*`); Feast materialized through the synced Secret; readiness and a real lookup: HTTP 200, 3 features |
| Rollback | Switch to `feast-online-redis` → HTTP 200; switch back → HTTP 200 |
| Second namespace | `ewallet-agents` shadow Secret synced, no consumer |
| Isolation (all denied, no target Secret) | ml-platform → Fraud API path and agent path: `permission denied`; ewallet-agents → Feature API path: `permission denied`; ewallet-agents store using the ml-platform role: `namespace not authorized` |
| Other negative cases | Application ServiceAccount: `service account name not authorized`; audience `kubernetes`: `invalid audience`; untrusted CA: `x509`; wrong auth mount: denied; missing Vault secret and missing property: not synced; invalid KV version: rejected at admission; consumer with a missing Secret: `CreateContainerConfigError` |
| NetworkPolicy | A listed pod reaches Vault; an unlisted pod in the same namespace cannot |
| Rotation | KV version 2 → ESO synced the new value → rollout restart → lookup HTTP 200 on the rotated (synthetic) Redis password |
| Vault unavailable | Scaled to 0: ExternalSecret `Ready False` (unreachable); Secret retained; the Feature API restarted on it and served HTTP 200 |
| Seal and persistence | Restart → sealed; sync fails (sealed); manual unseal → version 2 still present from the PVC → sync resumed |
| Audit | 436 lines, 126 Kubernetes logins, 0 plaintext matches of any synthetic value |

Local means local: kind's CNI (kindnet) enforced the NetworkPolicy, the Redis was a
disposable in-namespace instance rather than `feature-store/redis`, and the run used a
self-signed CA instead of cert-manager. None of this is a GKE result.

Static contract tests: [`tests/ops/test_vault_eso.py`](../../../tests/ops/test_vault_eso.py).
The earlier direct-loader runs remain in `docs/evidence/edai2/vault/local_20261106T183340Z` and `local_r2_20261107T101251Z`.

### Owner evidence on GKE (after an approved campaign)

Screenshots illustrate the deployed system; they are taken by the owner, never generated.

| Evidence | Shows |
|---|---|
| `ops/vault/cli.py status` | Vault 2.0.4, `initialized true`, `sealed false` after the manual unseal |
| `ops/vault/cli.py versions --path feature-api/feast-redis` and the policy files | KV v2 metadata (version, time, no value) and the ACL |
| `kubectl -n ml-platform get secretstore,externalsecret` | `Valid` / `SecretSynced`, Ready `True` |
| Feature API `secretKeyRef` and one real lookup | the consumer reads `feature-api-redis-vault` |
| A labelled negative ExternalSecret for `fraud-api/feast-redis` | `permission denied`, no target Secret |
| Optional: rotation (version 2, `force-sync`, restart) and a seal/unseal cycle | lifecycle |
