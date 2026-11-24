# Vault and External Secrets Operator: deployment, migration and recovery

Operating procedure for [VAULT.md](../../edai2/platform/VAULT.md). Each step is labelled:

- **READ-ONLY:** reads state; changes nothing.
- **MUTATING (Kubernetes):** creates or changes cluster objects.
- **MUTATING (Vault):** changes Vault state or secret data.
- **OWNER-ONLY:** handles recovery material; the owner runs it personally, at the laptop.

**Status:** implemented in Git and verified end to end on kind with
`scripts/vault/eso_local_validate.sh` (*Local validation*). **Not deployed on GKE, and not
yet accepted as row 54 evidence.** A GKE run needs a separately approved campaign: GKE is woken
and put back to sleep only by that campaign's approved cloud-lab procedure, never by a command
from this page.

Never print, paste or commit a Vault token, an unseal share, a private key or a secret value.
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

## 1. Prerequisites (READ-ONLY)

Run the committed preflight first. It checks every item below that can be checked, and runs only read-only commands:

```bash
python3 scripts/vault/gke_preflight.py --kubeconfig $KUBECONFIG --context $CTX --out <campaign-dir>/preflight
# last line: {"verdict": "VAULT_GKE_PREFLIGHT_READY", "blocking": [], "not_checked": [...]}
```

It refuses (BLOCKED) if an earlier Vault or ESO namespace, CRD, release or PVC exists, or if
the `ewallet-serving` release differs from the repository chart. It also refuses if the overlay
would change anything besides the Feature API Secret name. While the node pool is at 0,
`cert-manager-ready` stays NOT_CHECKED; rerun the preflight after the wake.

- Approved campaign, GKE awake, laptop on the approved network.
- cert-manager running: `kubectl --context $CTX get crd certificates.cert-manager.io && kubectl --context $CTX -n cert-manager get pods`.
- No earlier installation: `kubectl --context $CTX get ns ewallet-vault external-secrets` returns NotFound for both.
- The Secrets being shadowed exist (names only): `kubectl --context $CTX -n ml-platform get secret feast-online-redis -o name`.
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
kubectl --context $CTX apply -f infra/kubernetes/vault/namespace.yaml -f infra/kubernetes/vault/certificate.yaml
kubectl --context $CTX -n ewallet-vault wait --for=condition=Ready certificate/vault-server-tls --timeout=120s
helm --kube-context $CTX install vault $CHARTS/vault-0.34.1.tgz -n ewallet-vault -f infra/kubernetes/vault/values-gke.yaml
kubectl --context $CTX apply -f infra/kubernetes/vault/network-policy.yaml -f infra/kubernetes/vault/admin-serviceaccount.yaml
kubectl --context $CTX -n ewallet-vault wait --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=240s
kubectl --context $CTX -n ewallet-vault get pvc        # READ-ONLY: data-vault-0 (5Gi) and audit-vault-0 (1Gi) Bound
kubectl --context $CTX -n ewallet-vault port-forward pod/vault-0 8200:8200 &   # keep open; restart it after a Vault restart
kubectl --context $CTX -n ewallet-vault get secret vault-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > $KEYDIR/ca.crt
$V status        # READ-ONLY: {"initialized": false, "sealed": true, ...}
```

`standard-rwo` deletes a disk when its PVC is deleted (`reclaimPolicy: Delete`). Recommended,
with owner approval: keep the two Vault disks even if their PVCs are deleted by mistake
(**MUTATING**, reversible):

```bash
for pv in $(kubectl --context $CTX -n ewallet-vault get pvc -o jsonpath='{.items[*].spec.volumeName}'); do
  kubectl --context $CTX patch pv "$pv" -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
done
```

The readiness probe treats sealed and uninitialised as Ready (`sealedcode=204&uninitcode=204`).
Without that, the port-forward needed for `init` could never open. **Pod Ready therefore does
not mean usable.** Every gate below checks `initialized: true, sealed: false` from `$V status`.

## 3. Initialise (MUTATING, Vault; OWNER-ONLY)

```bash
$V init --keys-out $KEYDIR/vault-keys.json     # 3 shares, threshold 2; refuses an existing file or a path in the repo
```

`vault-keys.json` (mode 0600) holds **all three unseal shares and the initial root token**.
It is the most sensitive file in the project:

1. Copy it at once to the two offline encrypted media. Never to cloud sync, chat, Git or evidence.
2. The root token inside it is used only for step 4, then revoked (*Token lifecycle*).
3. Optional: give each share to a different holder. With one owner, the shares sit together,
   so Shamir protects against loss of the cluster, not against loss of that file. This is a
   known limitation of the single-owner lab (*Recovery material* in VAULT.md).

Losing the shares means losing the Vault data; there is no recovery path. Never initialise
again over existing data: `init` refuses an initialised Vault.

## 4. Unseal and configure (MUTATING, Vault)

```bash
$V unseal --keys-file $KEYDIR/vault-keys.json       # {"sealed": false}
$V status                                           # gate: initialized true, sealed false
$V configure --keys-file $KEYDIR/vault-keys.json    # audit, KV v2 "ewallet", Kubernetes auth, policies, roles
```

Then replace the root token with the scoped operator token, and revoke root:

```bash
$V operator-token --keys-file $KEYDIR/vault-keys.json --token-out $OP   # orphan, policy secret-operator, TTL 720h
$V retire-root --keys-file $KEYDIR/vault-keys.json                      # {"root_token_revoked": true}
$V versions --keys-file $KEYDIR/vault-keys.json --path feature-api/feast-redis   # must now fail: REFUSED vault_admin_failed
```

`configure` applies [bootstrap.json](../../../infra/kubernetes/vault/bootstrap.json) idempotently:

| Vault role | Bound identity | Policy reads |
|---|---|---|
| `eso-ml-platform` | `ml-platform/eso-vault-reader`, audience `vault` | `ewallet/data/feature-api/feast-redis` |
| `eso-ewallet-agents` (staged) | `ewallet-agents/eso-vault-reader`, audience `vault` | `ewallet/data/agent-platform/agentgateway-client-key` |
| `feature-api`, `fraud-api` | direct-loader roles, kept, not used by ESO | their own path |
| `vault-admin` | `ewallet-vault/vault-admin` (the owner's admin login), 15 min | nothing: policy `root-generation` only |

## 5. Seed Vault from the existing Secret (MUTATING, Vault)

The value goes from the existing Secret straight into Vault on stdin, without being printed:

```bash
kubectl --context $CTX -n ml-platform get secret feast-online-redis -o jsonpath='{.data.connection_string}' | base64 -d \
  | $V put --token-file $OP --path feature-api/feast-redis --key connection_string --stdin
$V versions --token-file $OP --path feature-api/feast-redis   # metadata only: current_version 1
```

Staged Agent Platform path (only if that part is approved). Use a **new synthetic** key, never the live gateway key:

```bash
openssl rand -hex 24 | $V put --token-file $OP --path agent-platform/agentgateway-client-key --key api-key --stdin
```

## 6. External Secrets Operator (MUTATING, Kubernetes)

```bash
kubectl --context $CTX -n ml-platform create configmap vault-ca --from-file=ca.crt=$KEYDIR/ca.crt
kubectl --context $CTX apply -f infra/kubernetes/external-secrets/namespace.yaml
helm --kube-context $CTX install external-secrets $CHARTS/external-secrets-2.14.0.tgz -n external-secrets \
  -f infra/kubernetes/external-secrets/values.yaml --wait --timeout 5m
kubectl --context $CTX -n external-secrets get deploy      # READ-ONLY: 3 deployments, 1/1 each
```

ESO processes only namespaced `SecretStore`s. Cluster-wide stores and PushSecrets are ignored
(`values.yaml`). Vault's NetworkPolicy admits the ESO controller pod and no other pod in `external-secrets`.

## 7. Shadow synchronisation (MUTATING, Kubernetes)

ESO creates a **new** Secret. The Feature API still reads `feast-online-redis`.

```bash
kubectl --context $CTX apply -f infra/kubernetes/external-secrets/ml-platform.yaml
kubectl --context $CTX -n ml-platform wait --for=condition=Ready secretstore/vault --timeout=120s
kubectl --context $CTX -n ml-platform wait --for=condition=Ready externalsecret/feature-api-redis --timeout=120s
```

Read-only validation. The comparison prints one status, never a value or a hash:

```bash
kubectl --context $CTX -n ml-platform get externalsecret feature-api-redis      # STATUS SecretSynced, READY True
kubectl --context $CTX -n ml-platform get secret feature-api-redis-vault -o json | jq -c '.data | keys'   # ["connection_string"]
bash scripts/vault/shadow_compare.sh $CTX ml-platform feast-online-redis feature-api-redis-vault
```

[`shadow_compare.sh`](../../../scripts/vault/shadow_compare.sh) exits 0 only with
`SHADOW_MATCH`: both Secrets exist, both have a non-empty `connection_string`, every kubectl,
jq and base64 step succeeded, and the values are equal. Anything else prints its own status and
exits 1: `SHADOW_MISMATCH`, `SHADOW_<EXISTING|SHADOW>_SECRET_MISSING`, `…_KEY_MISSING`,
`…_VALUE_EMPTY` or `…_READ_FAILED` (for example, an API error).

**Gate:** proceed only on `SHADOW_MATCH` with exit code 0.

## 8. Consumer switch (MUTATING, Kubernetes; production release)

```bash
helm --kube-context $CTX -n ml-platform history ewallet-serving --max 1      # note the revision for rollback
helm upgrade ewallet-serving infra/helm/ewallet-serving --kube-context $CTX -n ml-platform --reuse-values \
  -f infra/helm/ewallet-serving/values-vault-eso.yaml
kubectl --context $CTX -n ml-platform rollout status deployment/feature-api --timeout=300s
kubectl --context $CTX -n ml-platform get deploy feature-api \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="EWALLET_REDIS_CONNECTION_STRING")].valueFrom.secretKeyRef.name}'
# feature-api-redis-vault
```

The readiness probe (`/health/ready`) reads the Feast registry and Redis, so Ready already
shows the credential works. Then run one real lookup through the edge or a port-forward
(the Feature API section of [EXTERNAL_GATEWAY_RUNBOOK.md](EXTERNAL_GATEWAY_RUNBOOK.md)).
The overlay keeps `featureApi.vault.enabled=false`: the pod never talks to Vault.

## 9. Rollback (MUTATING, Kubernetes)

```bash
helm --kube-context $CTX -n ml-platform rollback ewallet-serving <REVISION-FROM-STEP-8>
# or: helm upgrade ewallet-serving infra/helm/ewallet-serving --kube-context $CTX -n ml-platform --reuse-values \
#       --set featureApi.redisSecret.name=feast-online-redis
kubectl --context $CTX -n ml-platform rollout status deployment/feature-api --timeout=300s
```

`feast-online-redis` is never changed by this procedure, so rollback restores the exact
earlier state. Leave the ExternalSecret in place, or delete it after rollback: it owns
`feature-api-redis-vault` and deleting it deletes that Secret. Never delete
`feast-online-redis`. After a real Redis password rotation, update `feast-online-redis` too,
or it stops being a valid rollback target.

## 10. Verification after deployment (READ-ONLY, except the negative test objects)

| Check | Command | Expected |
|---|---|---|
| Vault runtime | `$V status` | `initialized: true`, `sealed: false` |
| KV metadata | `$V versions --token-file $OP --path feature-api/feast-redis` | version and time, no value |
| ACL | `configure` output lists the applied policies; the text is `infra/kubernetes/vault/policies/eso-*.hcl` | one `read` path plus the two self-token paths; enforcement is shown by the denied-access row |
| Store | `kubectl -n ml-platform get secretstore vault` | `Valid`, Ready `True` |
| Sync | `kubectl -n ml-platform get externalsecret feature-api-redis` | `SecretSynced`, Ready `True` |
| Consumer | `secretKeyRef` of `deployment/feature-api`, one lookup | `feature-api-redis-vault`, HTTP 200 |
| Denied access | apply a labelled ExternalSecret `neg-fraud` in `ml-platform` with key `fraud-api/feast-redis`, then `kubectl -n ml-platform get events --field-selector involvedObject.name=neg-fraud` | Ready `False`, `permission denied`, no target Secret; delete it afterwards |

## 11. Rotation (MUTATING, Vault and Kubernetes)

1. Write the new value as a new KV version: `… | $V put --token-file $OP --path feature-api/feast-redis --key connection_string --stdin`.
2. ESO picks it up within `refreshInterval` (1h), or at once:
   `kubectl --context $CTX -n ml-platform annotate externalsecret feature-api-redis force-sync=$(date +%s) --overwrite`.
3. Check that ESO synced: `kubectl … get externalsecret feature-api-redis` shows `SecretSynced` after the force-sync, and `$V versions --token-file $OP …` shows the new version. Until `feast-online-redis` is updated too, `shadow_compare.sh` reports `SHADOW_MISMATCH`, which is correct.
4. The Feature API reads the variable at start-up: `kubectl --context $CTX -n ml-platform rollout restart deployment/feature-api`.

Changing the real Redis password is a separate, coordinated step (Redis, every client Secret,
then restarts). This runbook does not rotate it.

## Token lifecycle and later administration

Vault 2.0 requires authentication for root generation, to stop key-update requests with bogus
key fragments (HashiCorp, *Important changes*: "Previously unauthenticated endpoints require
authentication"). This deployment keeps that default: `values-gke.yaml` has **no** `enable_unauthenticated_access`.

| Credential | Created by | Used for | Ends |
|---|---|---|---|
| Unseal shares (in `vault-keys.json`) | `init` | unseal after every restart; completing a root-generation ceremony (2 of 3) | never; offline copies |
| Initial root token (in `vault-keys.json`) | `init` | step 4 only: `configure`, `operator-token` | `retire-root --keys-file` at the end of step 4 |
| Operator token (`$OP`) | `operator-token` | `put` (create or rotate), `versions` (metadata) | TTL 720 h, not renewable |
| Admin login (`$KEYDIR/admin-login.json`) | `login --role vault-admin` with a ServiceAccount token from `kubectl create token` | starting and feeding a root-generation ceremony, nothing else | 15 min |
| Temporary root (`$KEYDIR/admin-token.json`) | `generate-root` (admin login + 2 shares) | policy and role changes, a new operator token | `retire-root --token-file` right after use |

Who can do what:

- The operator token (orphan, `secret-operator` only) writes values and reads metadata under
  `feature-api/`, `fraud-api/` and `agent-platform/`. It cannot read values, change policies,
  roles or auth methods, or generate a root token.
- The admin login can only run the root-generation ceremony (`policies/root-generation.hcl`).
  Finishing it needs 2 unseal shares. Getting the login needs permission to create tokens for
  `ewallet-vault/vault-admin`, which cluster administrators have. That identity never expires,
  so there is no dependency cycle.
- ESO reader roles and application roles have no `sys/` path at all (tested).

**Policy or role change** (for example, after editing `bootstrap.json` or a policy file). Needs Vault unsealed:

```bash
kubectl --context $CTX -n ewallet-vault create token vault-admin --audience vault --duration 10m \
  | $V login --role vault-admin --token-out $KEYDIR/admin-login.json
$V generate-root --keys-file $KEYDIR/vault-keys.json --token-file $KEYDIR/admin-login.json --token-out $KEYDIR/admin-token.json
$V configure --token-file $KEYDIR/admin-token.json
$V retire-root --token-file $KEYDIR/admin-token.json
rm $KEYDIR/admin-login.json $KEYDIR/admin-token.json
```

`generate-root` cancels any unfinished attempt first. If the shares run out it cancels its own
attempt and fails, and every API error stops it.

**Operator token expired or lost:** run the first two commands above, then
`$V operator-token --token-file $KEYDIR/admin-token.json --token-out $KEYDIR/operator-token-<date>.json`,
then `retire-root --token-file` and the `rm`. Token files are never overwritten, so each new
token gets a new file name. A lost token stays valid until its TTL ends, unless an operator
revokes it through the Vault API (`auth/token/revoke`) with the temporary root. This CLI has no command for that.

**After a long GKE sleep:** tokens expire by wall-clock time, so the operator token may have
expired while the cluster slept. Unseal (step 12), then replace the operator token as above.
The admin login does not depend on any stored token. ESO is unaffected: it logs in afresh for every sync.

**Break-glass (owner approval required; not part of normal operation).** Use this only if the
admin login itself cannot work, for example if Vault's Kubernetes auth configuration is broken
or the `vault-admin` role was deleted. In that case no authenticated identity can start root generation:

1. Record the approval and the reason in the campaign evidence.
2. Temporarily allow unauthenticated root generation:
   `helm --kube-context $CTX upgrade vault $CHARTS/vault-0.34.1.tgz -n ewallet-vault -f infra/kubernetes/vault/values-gke.yaml -f <break-glass.yaml>`.
   `<break-glass.yaml>` is a local, uncommitted copy of the `server.ha.raft.config` block with
   `enable_unauthenticated_access = ["generate-root"]` added. Then restart the pod
   (`kubectl --context $CTX -n ewallet-vault delete pod vault-0`), and unseal.
3. Generate a root token with the shares. The direct Vault API (`sys/generate-root/attempt`
   and `sys/generate-root/update`) is used here, because this CLI always requires an admin
   login. Repair Kubernetes auth or the role with `configure --token-file`, then `retire-root`.
4. **Restore the secure configuration at once:** `helm upgrade` with `values-gke.yaml` only,
   restart `vault-0`, unseal, and confirm that `sys/generate-root/attempt` without a token
   answers 403.

This path has not been exercised on kind. The behaviour of the setting itself was checked on
a disposable Vault 2.0.4 container: without a token, 403 when the setting is absent; started
when it is present.

Token files live only in `$KEYDIR` (mode 0600; the CLI refuses a path inside the repository).
No command prints a token.

## 12. GKE sleep and wake

- **Sleep:** use the campaign's approved cloud-lab sleep procedure. Nothing Vault-specific is
  added to it. Vault has no PodDisruptionBudget (`server.ha.disruptionBudget.enabled: false`).
  The chart's default budget for one replica is `maxUnavailable: 0`, which the sleep tool's
  check (`evaluate_pdbs`, `minAvailable` only) would not flag, and which would then stall the
  resize to zero. The drain therefore evicts `vault-0` like any other pod. Vault seals as it
  stops; Raft data is on the PVC. The ESO Deployments have no budget either. **Never delete the
  `ewallet-vault` namespace or its PVCs while sleeping**, and do not `helm uninstall` Vault or
  ESO: sleep keeps both installed.
- **Disks while asleep:** the data (5 GiB) and audit (1 GiB) disks are zonal in
  `asia-southeast1-b`, the only zone of `cpu-pool`, so after a wake `vault-0` reattaches them on
  any new node. Check after the sleep: `kubectl --context $CTX -n ewallet-vault get pvc` shows both Bound.
- **Wake:** Vault comes back **sealed**. Until it is unsealed, ESO cannot sync (`ExternalSecret`
  Ready `False`), but the synced Secrets stay in place. The Feature API therefore starts and
  serves normally, unlike the direct loader, which fails closed while Vault is sealed. After the
  wake, open the port-forward and run `$V unseal --keys-file $KEYDIR/vault-keys.json` and `$V status`,
  then force a sync (step 11.2) and check `SecretSynced`. If the operator token expired during
  the sleep, replace it (*Token lifecycle*).
- A retained Secret is not proof that ESO is syncing. Check the ExternalSecret condition.
- **Certificate renewal:** cert-manager renews `vault-server-tls` 15 days before its 90-day
  expiry, but a running Vault keeps serving the certificate it loaded at start-up. Each wake
  restarts Vault, so this matters only if Vault runs for more than 75 days without a restart.
  Check `kubectl --context $CTX -n ewallet-vault get certificate vault-server-tls` (`Ready`, renewal
  time). After a renewal, restart `vault-0` and unseal it. The `vault-ca` CA lasts one year;
  when it is renewed, re-publish `ca.crt` to the `vault-ca` ConfigMaps (step 6).

## 13. Safe cleanup (MUTATING, Kubernetes)

Only on a separate decision; the order matters:

1. Roll the consumer back (step 9).
2. `kubectl --context $CTX delete -f infra/kubernetes/external-secrets/ml-platform.yaml` (deletes `feature-api-redis-vault`).
3. `helm --kube-context $CTX uninstall external-secrets -n external-secrets`. The chart owns the
   ESO CRDs (`installCRDs: true`), so uninstalling deletes them, and with them **every**
   SecretStore and ExternalSecret in every namespace. Kubernetes then garbage-collects every
   Secret those ExternalSecrets own (`creationPolicy: Owner`). Never uninstall ESO while a
   workload still reads an ESO-owned Secret. `helm upgrade` keeps the CRDs and the objects.
4. `helm --kube-context $CTX uninstall vault -n ewallet-vault`. **The PVCs remain.** Deleting
   them, or the namespace, destroys all Vault data: that needs its own owner decision, after a
   Raft snapshot.

## Recovery from an incomplete installation

| Where it stopped | State | Recovery |
|---|---|---|
| Step 2: Vault install or Certificate | No data yet | Fix the cause; `helm uninstall vault -n ewallet-vault`; delete the empty PVCs only if `$V status` never showed `initialized: true`; retry |
| Step 3: `init` failed | `$V status` shows `initialized: false` and no keys file was written | Retry `init` |
| Step 3: Vault initialised but the keys file is missing | Data sealed, no shares | Unrecoverable by design. Vault holds no secrets yet, so the owner may decide to uninstall, delete the PVCs and start again |
| Step 4: `configure` stopped part-way | Some policies or roles applied | Re-run `configure`: every step is idempotent. If root is already retired, use the admin path (*Token lifecycle*) |
| Step 4: root retired before the operator token was created | No routine token | Admin path, then `operator-token` |
| Step 6: ESO install failed | Partial release | `helm uninstall external-secrets -n external-secrets` (nothing depends on it yet), then retry |
| Step 7: ExternalSecret not `SecretSynced` | No target Secret; Feature API unchanged | Read the cause in `kubectl -n external-secrets logs deploy/external-secrets` (ESO events are generic); fix the store, policy or value; force a sync. Do not switch the consumer |
| Step 8: Feature API not Ready after the switch | Old pods still serve, or the rollout is stuck | Rollback (step 9) |

## Local validation

```bash
helm pull vault --repo https://helm.releases.hashicorp.com --version 0.34.1 -d <dir>
helm pull external-secrets --repo https://charts.external-secrets.io --version 2.14.0 -d <dir>
docker build -f ai_platform/inference/Dockerfile -t localhost:5001/ewallet-inference:vault-eso-local . && docker push localhost:5001/ewallet-inference:vault-eso-local
# seed image, local only: the same image plus tests/ (the Feast fixtures)
printf 'FROM localhost:5001/ewallet-inference:vault-eso-local\nCOPY tests /app/tests\n' \
  | docker build -f - -t localhost:5001/ewallet-feast-seed:vault-eso-local . && docker push localhost:5001/ewallet-feast-seed:vault-eso-local
bash scripts/vault/eso_local_validate.sh <dir>/vault-0.34.1.tgz <dir>/external-secrets-2.14.0.tgz \
  localhost:5001/ewallet-inference@sha256:<digest> localhost:5001/ewallet-feast-seed@sha256:<digest> <evidence-dir>
```

The script refuses a non-kind context (default `kind-llm-agent-local`). It also refuses
pre-existing `ewallet-vault`, `external-secrets` or `ml-platform` namespaces, and removes
everything it created on exit.
