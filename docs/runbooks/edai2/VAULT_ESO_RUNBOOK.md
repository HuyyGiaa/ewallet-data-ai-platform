# Vault and External Secrets Operator: deployment, migration and recovery

Operating procedure for [VAULT.md](../../edai2/platform/VAULT.md). Each step is labelled:

- **READ-ONLY:** reads state; changes nothing.
- **MUTATING (Kubernetes):** creates or changes cluster objects.
- **MUTATING (Vault):** changes Vault state or secret data.
- **OWNER-ONLY:** handles recovery material; the owner runs it personally, at the laptop.

**Nothing in this runbook has run on GKE.** It was validated end to end on kind with
`scripts/vault/eso_local_validate.sh` (*Local validation*). A GKE run needs a separately
approved campaign: GKE is woken and put back to sleep only by that campaign's approved
cloud-lab procedure, never by a command from this page.

Never print, paste or commit a Vault token, an unseal share, a private key or a secret value.
Do not use `set -x`. Values go into Vault on stdin (`put --stdin`) or a hidden prompt, never as an argument.

## Variables

```bash
CTX=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev   # pin every command
KEYDIR=~/ewallet-evidence/vault/private      # outside the repository, mode 700; ops/vault refuses a path inside it
CHARTS=~/ewallet-evidence/vault/charts       # pulled charts, digests checked below
V="python3 -m ops.vault.cli --ca-file $KEYDIR/ca.crt"   # talks to https://127.0.0.1:8200 (port-forward)
```

## 1. Prerequisites (READ-ONLY)

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
kubectl --context $CTX apply -f infra/kubernetes/vault/network-policy.yaml
kubectl --context $CTX -n ewallet-vault wait --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=240s
kubectl --context $CTX -n ewallet-vault port-forward pod/vault-0 8200:8200 &   # keep open; restart it after a Vault restart
kubectl --context $CTX -n ewallet-vault get secret vault-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > $KEYDIR/ca.crt
$V status        # READ-ONLY: {"initialized": false, "sealed": true, ...}
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
2. After step 6 is verified, retire the root token: `$V retire-root --keys-file $KEYDIR/vault-keys.json`.
   A new root token can be generated from the unseal shares (`vault operator generate-root`) only when needed.
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

`configure` applies [bootstrap.json](../../../infra/kubernetes/vault/bootstrap.json) idempotently:

| Vault role | Bound identity | Policy reads |
|---|---|---|
| `eso-ml-platform` | `ml-platform/eso-vault-reader`, audience `vault` | `ewallet/data/feature-api/feast-redis` |
| `eso-ewallet-agents` (staged) | `ewallet-agents/eso-vault-reader`, audience `vault` | `ewallet/data/agent-platform/agentgateway-client-key` |
| `feature-api`, `fraud-api` | direct-loader roles, kept, not used by ESO | their own path |

## 5. Seed Vault from the existing Secret (MUTATING, Vault)

The value goes from the existing Secret straight into Vault on stdin, without being printed:

```bash
kubectl --context $CTX -n ml-platform get secret feast-online-redis -o jsonpath='{.data.connection_string}' | base64 -d \
  | $V put --keys-file $KEYDIR/vault-keys.json --path feature-api/feast-redis --key connection_string --stdin
$V versions --keys-file $KEYDIR/vault-keys.json --path feature-api/feast-redis   # metadata only: current_version 1
```

Staged Agent Platform path (only if that part is approved). Use a **new synthetic** key, never the live gateway key:

```bash
openssl rand -hex 24 | $V put --keys-file $KEYDIR/vault-keys.json --path agent-platform/agentgateway-client-key --key api-key --stdin
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

Read-only validation. It prints only whether the two values are equal:

```bash
kubectl --context $CTX -n ml-platform get externalsecret feature-api-redis      # STATUS SecretSynced, READY True
kubectl --context $CTX -n ml-platform get secret feature-api-redis-vault -o json | jq -c '.data | keys'   # ["connection_string"]
a=$(kubectl --context $CTX -n ml-platform get secret feast-online-redis -o jsonpath='{.data.connection_string}' | sha256sum)
b=$(kubectl --context $CTX -n ml-platform get secret feature-api-redis-vault -o jsonpath='{.data.connection_string}' | sha256sum)
[ "$a" = "$b" ] && echo SHADOW_MATCH || echo SHADOW_MISMATCH
```

**Gate:** proceed only on `SHADOW_MATCH`.

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
| KV metadata | `$V versions --keys-file … --path feature-api/feast-redis` | version and time, no value |
| ACL | `configure` output lists the applied policies; the text is `infra/kubernetes/vault/policies/eso-*.hcl` | one `read` path plus the two self-token paths; enforcement is shown by the denied-access row |
| Store | `kubectl -n ml-platform get secretstore vault` | `Valid`, Ready `True` |
| Sync | `kubectl -n ml-platform get externalsecret feature-api-redis` | `SecretSynced`, Ready `True` |
| Consumer | `secretKeyRef` of `deployment/feature-api`, one lookup | `feature-api-redis-vault`, HTTP 200 |
| Denied access | apply a labelled ExternalSecret `neg-fraud` in `ml-platform` with key `fraud-api/feast-redis`, then `kubectl -n ml-platform get events --field-selector involvedObject.name=neg-fraud` | Ready `False`, `permission denied`, no target Secret; delete it afterwards |

## 11. Rotation (MUTATING, Vault and Kubernetes)

1. Write the new value as a new KV version: `… | $V put --keys-file … --path feature-api/feast-redis --key connection_string --stdin`.
2. ESO picks it up within `refreshInterval` (1h), or at once:
   `kubectl --context $CTX -n ml-platform annotate externalsecret feature-api-redis force-sync=$(date +%s) --overwrite`.
3. Check that the Secret changed: the step 7 hash comparison against the new value, or `$V versions`.
4. The Feature API reads the variable at start-up: `kubectl --context $CTX -n ml-platform rollout restart deployment/feature-api`.

Changing the real Redis password is a separate, coordinated step (Redis, every client Secret,
then restarts). This runbook does not rotate it.

## 12. GKE sleep and wake

- **Sleep:** nothing Vault-specific. The Vault PVCs (Persistent Disks) survive the node pool
  scaling to 0. Never delete the `ewallet-vault` namespace or its PVCs during cleanup: that
  destroys all Vault data. The ESO Deployments and Vault have no PodDisruptionBudget.
- **Wake:** Vault comes back **sealed**. Until it is unsealed, ESO cannot sync (`ExternalSecret`
  Ready `False`), but the synced Secrets stay in place. The Feature API therefore starts and
  serves normally, unlike the direct loader, which fails closed while Vault is sealed. After the
  wake, open the port-forward and run `$V unseal --keys-file $KEYDIR/vault-keys.json` and `$V status`,
  then force a sync (step 11.2) and check `SecretSynced`.
- A retained Secret is not proof that ESO is syncing. Check the ExternalSecret condition.

## 13. Safe cleanup (MUTATING, Kubernetes)

Only on a separate decision; the order matters:

1. Roll the consumer back (step 9).
2. `kubectl --context $CTX delete -f infra/kubernetes/external-secrets/ml-platform.yaml` (deletes `feature-api-redis-vault`).
3. `helm --kube-context $CTX uninstall external-secrets -n external-secrets` (removes the ESO CRDs, and with them any remaining ExternalSecret objects).
4. `helm --kube-context $CTX uninstall vault -n ewallet-vault`. **The PVCs remain.** Deleting
   them, or the namespace, destroys all Vault data: that needs its own owner decision, after a
   Raft snapshot.

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
