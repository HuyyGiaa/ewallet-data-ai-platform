# R2: move the serving APIs off the shared `training` identity

Source is ready on `integration/g0-20261107`. **Nothing here has run.** Each step marked MUTATING needs owner approval: Terraform apply, a Vault change or a Kubernetes apply.

## Target

| Workload | KSA | GSA | GCS | Vault |
|---|---|---|---|---|
| Fraud API | `ml-platform/fraud-api` | `ewallet-fraud-api` | `objectViewer`, condition `features/registries/` | role `fraud-api` |
| Feature API | `ml-platform/feature-api` | `ewallet-feature-api` | Same condition | role `feature-api`, optional |
| MLflow | `ml-platform/training` | `ewallet-training` | Unchanged | none |

The serving APIs read one object, the online registry, with `objects.get`. `registry_mirror.pull` never lists the bucket, so a prefix condition is enough.

## Is Feature API Vault onboarding a prerequisite?

**No. The ServiceAccount separation is. Vault for the Feature API can follow it.**

- **Running the Feature API as `training` is not acceptable.** It would give a public-facing API training's write access to the whole 3.4 TiB data bucket. Deploy it only as `feature-api` with its registry-only GSA.
- **The Redis connection string can stay in the `feast-online-redis` Secret**, as it does for the Fraud API before P7. This keeps the CPU serving campaign independent of Vault, which seals on every restart and adds an unseal step to each wake.
- **Onboarding Vault is a separate, later step**, below. The chart keeps the Secret path until `featureApi.vault.enabled=true`.

## Sequence

### 0. Readback (read-only)

```bash
kubectl -n ml-platform get deploy fraud-api -o jsonpath='{.spec.template.spec.serviceAccountName}{"\n"}'   # expect training
kubectl -n ml-platform get sa fraud-api feature-api                                                       # expect NotFound
gcloud iam service-accounts list --project "$PROJECT" --filter='email~ewallet-(fraud|feature)-api'         # expect none
gcloud storage buckets get-iam-policy "gs://$PROJECT-data" --format=json > "$EV/bucket-iam-before.json"
```

### 1. MUTATING (Terraform): GSAs, conditional bucket access and Workload Identity bindings

Modules `iam` and `workload_identity`. Review `terraform plan`. It must show only additions:
- 2 service accounts;
- 2 conditional `registry_reader` members;
- 2 `workloadIdentityUser` bindings.

### 2. MUTATING (Kubernetes): the KSAs

```bash
GCP_PROJECT_ID=$PROJECT envsubst '${GCP_PROJECT_ID}' < infra/kubernetes/fraud-serving/service-account.yaml.tmpl | kubectl apply -f -
# The Feature API account comes with the ewallet-serving chart (featureApi.serviceAccount).
```

### 3. Verify the identity before any rollout (read-only)

Run a throwaway pod as `fraud-api`, on a node with the GKE metadata server:
1. `gcloud auth list` must show `ewallet-fraud-api@…`.
2. A `gcloud storage cat` of the registry object must succeed.
3. A `cat` outside `features/registries/` must fail with 403.

Repeat for `feature-api`.

### 4. MUTATING: Fraud API rollout

Apply the manifest rendered by `ops.serving.fraud_api_manifest`, which now sets `serviceAccountName: fraud-api` and `automountServiceAccountToken: false`. Use the same mode as before; R1's handover is a separate step.

Check:
- the rollout completes;
- `/health/ready` returns 200, which proves the registry mirror loaded;
- a prediction succeeds.

### 5. MUTATING (Vault): roles bound to the new accounts

Requires an unsealed Vault and the operator token.

```bash
python -m ops.vault.cli configure ...   # applies bootstrap.json: role fraud-api -> SA fraud-api; role feature-api -> SA feature-api
```

Only after this is the Fraud API's Vault patch (P7) valid, because the role no longer accepts `training`.

If P7 is already live, apply this step **in the same window as step 4**. Otherwise a restarted Fraud API pod fails to log in.

### 6. MUTATING: Feature API deployment

`helm upgrade --install ewallet-serving` with:
- `featureApi.serviceAccount.gcpServiceAccount=ewallet-feature-api@$PROJECT.iam.gserviceaccount.com`;
- the Secret path; Vault stays off.

### 7. Later, MUTATING: Feature API Vault onboarding

1. Write `ewallet/data/feature-api/feast-redis` with the secret-operator token, which may write that path.
2. Apply `infra/kubernetes/vault/network-policy.yaml`, which admits `feature-api` pods.
3. `helm upgrade … --set featureApi.vault.enabled=true`.

## Verification after the move

1. `kubectl auth can-i --list --as=system:serviceaccount:ml-platform:fraud-api` must show only discovery and self-review rules. Repeat for `feature-api`.
2. A `fraud-api` pod presenting role `feature-api` must be denied, and the reverse too.

## Rollback

| Step | Rollback |
|---|---|
| 4 | `kubectl -n ml-platform patch deploy fraud-api --type=json -p '[{"op":"replace","path":"/spec/template/spec/serviceAccountName","value":"training"}]'`. `training` keeps its Workload Identity binding throughout |
| 5 | Re-apply the previous `bootstrap.json`, with role `fraud-api` bound to `training`, at the same time as rolling back step 4 |
| 6 | `helm rollback ewallet-serving` or uninstall; the Feature API is new |
| 1–2 | Leave the unused accounts and bindings in place, or remove them with Terraform. They grant registry read only |

## Local evidence

- `tests/ops/test_r2_serving_identity.py`: accounts, chart refusals, Vault roles and policies, network policy, no RBAC, Terraform mappings and conditions.
- `scripts/vault/local_validate.sh` on kind with the real Vault chart:
  - each account reads only its own path;
  - `training` is denied;
  - each account is denied the other's role.
