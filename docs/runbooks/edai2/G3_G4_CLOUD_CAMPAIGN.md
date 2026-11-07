# G3/G4: Cloud Build and GKE runtime campaign

Owner-reviewable packages that move the integrated platform onto GKE. **Nothing here has run.** Every step marked MUTATING needs the owner's approval of its package.

Source: branch `integration/g0-20261107`, at the commit that adds this file. It is local and unpushed. Build only from a clean checkout of that commit.

## What the read-only audit found (2026-11-07)

| Area | Fact |
|---|---|
| Cluster | `ewallet-dev`, zone `asia-southeast1-b`, GKE 1.36.4, REGULAR channel, Workload Identity on |
| Node pool | `cpu-pool`, `e2-standard-4`, **0 nodes, autoscaling off** (resized by `cloud_lab.py`) |
| Quota | E2 CPUs 0 / 24 (6 nodes); CPUs 0 / 100; `GPUS_ALL_REGIONS` 0; billing enabled |
| Scaled to 0 at sleep | `fraud-coordinator`, `feature-rag-specialist`, `drift-specialist`, `drift-mcp` (both namespaces), `vllm-fallback` |
| Helm | `llm-agent` r41, `llm-agent-mcp` r6, kagent 0.10.3, KServe 0.16.0, Istio 1.30.7, cert-manager 1.16.2, substrate 0.0.9 |
| Not installed | KEDA, namespaces `ewallet-edge` and `ewallet-vault` |
| Fraud API | 1 replica, SA `training`, client-side apply, image `git-7b095baa` |
| KServe | `fraud-detector` Ready, Standard mode, `autoscalerClass: none` |
| Secret | `ml-platform/feast-online-redis` exists (name only read) |
| PVCs | 15, all Bound, including Redis, kagent PostgreSQL, MLflow PostgreSQL, Valkey ×6 |
| Release drift | `release_state.py drift`: 0 problems |

## Images

**Rebuild 3 images.** Reuse every other image: its build inputs are unchanged, or it is not in Session A.

| Image | Build | Deployed digest (rollback) | Deployed source | Why |
|---|---|---|---|---|
| `fraud-api` (also serves the Feature API) | **Yes**: `ai_platform/inference/Dockerfile`, context `.`, project `ai_platform/inference` | `sha256:20e9c2ad1272d1564198dc970dd402ead17dbca0b1881b3119dca0c4c90e3165` | `7b095baa` | Feature API, Vault loader, ML A/B router: 17 files in `ai_platform/inference` |
| `g3-byo-coordinator` | **Yes**: `infra/docker/g3-byo-coordinator/Dockerfile` | `sha256:483dd943290c02fdd4d0d3e9e38b4182dae76a01581e6e5e191ca977b7498aff` | `0c2ae1d0` | R8 masking, Agent A/B, in-flight gauge, MCP client failover |
| `g3-byo-feature-rag-specialist` | **Yes**: same Dockerfile; the release keeps a separate repository | `sha256:5f848a1ce7942f4fd5027264bc54c3164d09065d3c8932fcd4a93bce8db57606` | `0c2ae1d0` | Same source |
| `mcp-tools` | No | `sha256:3209d01e514669af01a967ce37a3e909db5cd682bc066b267aaf82399761411f` (ewallet-mcp) | `1be6c3e7` | `ai_platform/llm_agent/tools` and `wire.py` unchanged |
| `fraud-detector-predictor` (v1) | No | `sha256:cba90c22787c672559ceaeba26ad9f23faa6eae3fbbdb83cb7c2e48f10b5b896` | `12b6c819` | `ai_platform/serving` unchanged; the model files are not in Git |
| `vllm-cpu-model` | No | `sha256:f695b23efa8145ae5cb57a1873863879927ab4805e70e5eee1a71a2063edd07c` | `fb8cab8a` | Unchanged |
| Candidate predictor v2 | Later, Session B | — | — | Model v2 not registered |
| `flink-block-a`, `streaming-sinks` | Later, C-A E2 | — | — | Not in Session A; never built before |

## Package 1: Cloud Build and image push

| Item | Detail |
|---|---|
| Affected | 3 images in `asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform`; source archives in `gs://…-cicd-builds`; logs in `gs://…-cicd-build-logs` |
| Identity | Builds run as `ewallet-cloudbuild`, which is the only writer of the repository. The owner, `roles/owner`, may act as it |
| Provenance | `git archive` of the exact SHA; pinned builder digest; label `org.opencontainers.image.revision=<sha>`; `requestedVerifyOption: VERIFIED`; tag `<name>:<40-char sha>`. Deployments use digests only |
| Cost | About 3 × 10 build-minutes on the default pool, inside the 2,500 free minutes a month (about $0.18 beyond it). About 1 GB more registry storage: about $0.10 a month |
| Risk | Low; nothing deployed changes |

```bash
cd .worktrees/main-integration
SHA=$(git rev-parse HEAD); test -z "$(git status --porcelain)" || { echo "dirty checkout"; exit 1; }
EV=docs/evidence/edai2/build/g3_$(date -u +%Y%m%dT%H%M%SZ)
python3 -m scripts.ci.cloud_build --git-sha "$SHA" \
  --image fraud-api=ai_platform/inference/Dockerfile \
  --image g3-byo-coordinator=infra/docker/g3-byo-coordinator/Dockerfile \
  --image g3-byo-feature-rag-specialist=infra/docker/g3-byo-coordinator/Dockerfile \
  --token-command 'gcloud auth print-access-token' \
  --evidence-dir "$EV" --output "$EV/image-manifest.json"
```

**Verify:**

```bash
REG=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform
for ref in $(jq -r '.images[].digest_ref' "$EV/image-manifest.json"); do
  gcloud artifacts docker images describe "$ref" --show-provenance --format=json > "$EV/provenance-$(basename "${ref%@*}").json"
  docker buildx imagetools inspect "$ref" --format '{{json .Image.Config.Labels}}' | jq -e --arg sha "$SHA" '."org.opencontainers.image.revision" == $sha'
done
```

**Vulnerability scan.** Container Scanning is not enabled, and Package 1 does not enable it. Run the repository's pinned Trivy against the remote image; it streams layers and builds nothing locally.

```bash
umask 077; printf 'TRIVY_USERNAME=oauth2accesstoken\nTRIVY_PASSWORD=%s\n' "$(gcloud auth print-access-token)" > "$HOME/.trivy-ar.env"
for ref in $(jq -r '.images[].digest_ref' "$EV/image-manifest.json"); do
  docker run --rm --env-file "$HOME/.trivy-ar.env" -v "$HOME/.cache/trivy:/root/.cache" -v "$PWD/$EV:/out" \
    docker.io/aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa \
    image --image-src remote --scanners vuln,secret --severity HIGH,CRITICAL --format json \
    -o "/out/trivy-$(basename "${ref%@*}").json" "$ref"
done
rm -f "$HOME/.trivy-ar.env"
```

**Stop if:**
- a build is not `SUCCESS`;
- a digest is missing;
- the revision label differs from `$SHA`;
- Trivy finds a secret;
- a CRITICAL finding is new compared with the deployed digest.

**Rollback:** nothing to undo. Old digests stay in the registry, and nothing points to the new ones until Package 4.

## Package 2: Terraform IAM (R2)

**Finding: an ordinary apply would replace the node pool.**

The dev state still records the pool with autoscaling 0–3. Live, autoscaling is off, and the cluster's authorized networks and network policy have drifted too. A read-only plan (`-lock=false`, variables taken from the state) gave:

| Plan | Result |
|---|---|
| Full plan | **node pool `delete,create`**, cluster `update`, plus the 6 R2 creates |
| `-target` on all 6 R2 resources | Same node-pool replacement, because `module.workload_identity` has `depends_on = [module.gke]` |
| `-target` on the 4 IAM resources | **Exactly 4 creates**: 2 GSAs and 2 bucket members conditioned on `features/registries/` |

So Terraform creates the 4 IAM resources only. The 2 Workload Identity bindings are added with `gcloud`, exactly one member each, then imported into the state. `ops.gcp.r2_terraform guard` refuses any plan that does more.

| Item | Detail |
|---|---|
| Affected | GSAs `ewallet-fraud-api`, `ewallet-feature-api`; 2 conditional `objectViewer` members on the data bucket; 2 `workloadIdentityUser` bindings |
| Not affected | GKE cluster, node pool, network, firewall, storage, any other IAM |
| Cost | None |
| Risk | Medium, because the state is shared. Mitigated by the guard, state lock and a backup |

```bash
cd .worktrees/main-integration/infra/terraform/gcp/environments/dev
P=ewallet-data-ai-platform-hg26; EV=$PWD/../../../../../docs/evidence/edai2/iam/r2_$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$EV"; umask 077
W=$(mktemp -d)
gcloud storage cat gs://$P-tfstate/terraform/dev/default.tfstate > "$W/state-before.json"      # private backup
gcloud storage buckets get-iam-policy gs://$P-data --format=json > "$EV/bucket-iam-before.json"
terraform init -input=false -backend-config="bucket=$P-tfstate" -backend-config="prefix=terraform/dev"
(cd ../../../../.. && python3 -m ops.gcp.r2_terraform tfvars --state "$W/state-before.json" --out "$OLDPWD/r2.auto.tfvars")
TG=(-target='module.iam.google_service_account.workload["fraud_api"]' -target='module.iam.google_service_account.workload["feature_api"]'
    -target='module.iam.google_storage_bucket_iam_member.registry_reader["fraud_api"]'
    -target='module.iam.google_storage_bucket_iam_member.registry_reader["feature_api"]')
terraform plan -input=false "${TG[@]}" -out="$W/r2.tfplan"
terraform show -json "$W/r2.tfplan" > "$W/r2.plan.json"
(cd ../../../../.. && python3 -m ops.gcp.r2_terraform guard --plan "$W/r2.plan.json")   # must print "exactly the approved R2 change"
terraform apply -input=false "$W/r2.tfplan"                                                # MUTATING
for sa in fraud-api feature-api; do                                                          # MUTATING, additive
  gcloud iam service-accounts add-iam-policy-binding "ewallet-$sa@$P.iam.gserviceaccount.com" --project $P \
    --role roles/iam.workloadIdentityUser --member "serviceAccount:$P.svc.id.goog[ml-platform/$sa]" --condition=None
  key=${sa//-/_}
  terraform import -input=false "module.workload_identity.google_service_account_iam_member.workload_identity[\"$key\"]" \
    "projects/$P/serviceAccounts/ewallet-$sa@$P.iam.gserviceaccount.com roles/iam.workloadIdentityUser serviceAccount:$P.svc.id.goog[ml-platform/$sa]"
done
rm -f r2.auto.tfvars; shred -u "$W"/* 2>/dev/null; rmdir "$W"
```

**Verify:**
- `gcloud iam service-accounts get-iam-policy` on each GSA shows exactly one `workloadIdentityUser` member.
- The bucket policy diff against `bucket-iam-before.json` holds only the 2 conditional members.
- `terraform state list | grep -E 'fraud_api|feature_api'` lists 6 addresses.

**Stop if:**
- the guard prints any STOP;
- the plan mentions `module.gke`;
- the apply reports anything but 4 additions.

**Rollback:**
1. `gcloud iam service-accounts remove-iam-policy-binding` for each binding, then `terraform state rm` of both addresses.
2. A 4-target `terraform destroy` with the same targets, guarded the same way. `training` stays bound throughout.

**Open item, not part of R2:** the GKE drift. Until it is reconciled in a separate decision, nobody runs an untargeted `terraform apply`.

## Package 3: Controlled GKE wake

**Capacity.** Requests recorded at sleep: the whole platform needs 8.9 vCPU and 22.7 GiB, which ran on 4 nodes.

| Profile | Nodes | List price per hour |
|---|---|---|
| Whole platform | 4 | $0.66 |
| Plus serving, KEDA and edge experiments | **5 (recommended)** | **$0.83** |
| Plus agent KEDA arms at 3 replicas | 6 (the E2 quota limit) | $0.99 |

The residual cost while asleep, about $0.107 an hour, is mostly bucket storage; it continues either way. An 8-hour working day on 5 nodes costs about $7.5.

```bash
cd .worktrees/main-integration
P=ewallet-data-ai-platform-hg26; M=docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z
gcloud billing projects describe $P --format='value(billingEnabled)'                       # must be True
gcloud compute regions describe asia-southeast1 --project $P --format=json | jq '.quotas[]|select(.metric=="E2_CPUS")'
gcloud container clusters get-credentials ewallet-dev --zone asia-southeast1-b --project $P --dns-endpoint
python3 ops/gcp/cloud_lab.py status
python3 ops/gcp/cloud_lab.py plan-wake --mode full --manifest "$M"
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest "$M" --nodes 5 \
  --execute --confirm-cluster ewallet-dev                                                  # MUTATING
python3 ops/gcp/cloud_lab.py verify --expect awake --manifest "$M"
```

`wake-full` restores the replica counts recorded at sleep and labels three nodes `ewallet.io/g3-llm=true`.

**Readiness checks:**
1. `kubectl get nodes`: 5 Ready.
2. `kubectl get pods -A | grep -vE 'Running|Completed'`: empty after 10 minutes.
3. Valkey: `kubectl -n ate-system exec valkey-cluster-0 -- valkey-cli cluster info | grep cluster_state:ok`. If not ok, follow the Cloud Lab runbook's CLUSTER MEET step.
4. `kubectl -n fraud-serving get isvc fraud-detector` is Ready.
5. `kubectl -n ml-platform rollout status deploy/fraud-api`.
6. `python3 scripts/ci/release_state.py drift --release llm-agent` reports 0 problems.
7. One synthetic A2A task through the Coordinator, with a synthetic UUID prompt and no PII, reaches `completed`.

**Stop if:**
- Pods stay Pending for capacity;
- a PVC fails to attach;
- KServe is not Ready;
- an image pull fails;
- a ServiceAccount is denied;
- any policy or Terraform change appears.

**Rollback:** `CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py sleep --scale-pdb-guarded --execute --confirm-cluster ewallet-dev`.

## Package 4: Serving deployment (Feature API, Fraud API)

**Prerequisites:** Packages 1–3. Images come from `$EV/image-manifest.json`.

```bash
FRAUD_IMAGE=$(jq -r '.images["fraud-api"].digest_ref' "$BUILD_EV/image-manifest.json")
REGISTRY=gs://ewallet-data-ai-platform-hg26-data/features/registries/canonical-100gb-v1/online/online-20261031-163506/registry.db
# R2 step 2: the Fraud API KSA (MUTATING)
GCP_PROJECT_ID=ewallet-data-ai-platform-hg26 envsubst '${GCP_PROJECT_ID}' \
  < infra/kubernetes/fraud-serving/service-account.yaml.tmpl | kubectl apply -f -
# R2 step 3: identity check (read-only pod, deleted after)
kubectl -n ml-platform run wi-check --rm -i --restart=Never --overrides='{"spec":{"serviceAccountName":"fraud-api","nodeSelector":{"iam.gke.io/gke-metadata-server-enabled":"true"}}}' \
  --image=gcr.io/google.com/cloudsdktool/google-cloud-cli:slim -- \
  sh -c "gcloud auth list --format='value(account)'; gcloud storage cat $REGISTRY >/dev/null && echo REGISTRY_OK; gcloud storage ls gs://ewallet-data-ai-platform-hg26-data/ml/ >/dev/null 2>&1 && echo OVERREACH || echo SCOPED"
# Fraud API: fixed mode, same owner (kubectl) as today (MUTATING)
FRAUD_API_IMAGE=$FRAUD_IMAGE ONLINE_REGISTRY_URI=$REGISTRY MODEL_VERSION=1 GIT_SHA=$SHA \
  python3 -m ops.serving.fraud_api_manifest --mode fixed --replicas 1 > /tmp/fraud-api.yaml
kubectl diff -f /tmp/fraud-api.yaml; kubectl apply -f /tmp/fraud-api.yaml
kubectl -n ml-platform rollout status deploy/fraud-api --timeout=300s
# Feature API with its own account (MUTATING)
helm upgrade --install ewallet-serving infra/helm/ewallet-serving -n ml-platform \
  --set featureApi.enabled=true --set featureApi.image="$FRAUD_IMAGE" --set featureApi.registryUri="$REGISTRY" \
  --set featureApi.serviceAccount.gcpServiceAccount=ewallet-feature-api@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com
kubectl -n ml-platform rollout status deploy/feature-api --timeout=300s
```

**Expected:**
- the check pod prints `ewallet-fraud-api@…`, `REGISTRY_OK` and `SCOPED`;
- `/health/ready` returns 200 on both APIs;
- a request built by `fraud_entities verify` returns 200 with `model_version: "1"`.

The image digest appears in pod status and in `EWALLET_GIT_SHA`.

**Stop if:**
- `OVERREACH` appears;
- readiness fails, meaning the registry mirror or Redis is not reachable;
- `kubectl diff` shows anything beyond image, SA, automount, `EWALLET_GIT_SHA` and the replica owner.

**Rollback:**
1. `kubectl -n ml-platform rollout undo deploy/fraud-api`, or re-render with the old digest and `serviceAccountName: training`.
2. `helm uninstall ewallet-serving -n ml-platform`.

## Package 5: Locust and KEDA

**Load (P-A2).** The fixture comes from the real training snapshot, 9 MiB:

```bash
SNAPSHOT=$(mktemp -d)/training-20261031-151638-ml2
gcloud storage cp -r gs://ewallet-data-ai-platform-hg26-data/ml/training-snapshots/canonical-100gb-v1/training-20261031-151638-ml2/split=test "$SNAPSHOT/"
```

Then run [SESSION_A_APPROVAL_PACKAGES.md](SESSION_A_APPROVAL_PACKAGES.md) P-A2, after one smoke run per target: `LOAD_RUN_TIME=1m … low`.

- Profiles: 5, 10 and 20 users, 5 minutes each.
- Each run writes HTML, CSV, history, failures, a manifest with SHA, digest, fixture and times, and `kubectl top` snapshots.

**Stop if:**
- failures exceed 5%;
- Redis or KServe restarts;
- the port-forward drops. In that case run Locust as an in-cluster Job and do not compare the two paths.

**Serving KEDA (P-A3).**
1. Install KEDA 2.21: `scripts/llm_agent/keda_install.sh`.
2. Run the fixed arms.
3. Hand over the Fraud API's replica count with `scripts/serving/fraud_api_keda_handover.sh plan`, then `handover --execute`.
4. Set `autoscaling.enabled=true` with thresholds from the fixed arms.
5. Run the KEDA arms, then `verify`.

**Never apply a `fixed` manifest to a Deployment that KEDA owns.** The KServe predictor gets no scaler (R3); record it as the expected bottleneck in A-02.

**Agent images and agent KEDA (P-A4).** Synthetic, PII-free prompts only, because of R8-P1. `release_state.py check` currently reports 2 non-image differences: the Grafana and Prometheus config checksums from the integrated chart. Image promotion is therefore two steps:

1. **MUTATING, chart refresh.** `helm upgrade llm-agent infra/helm/llm-agent-platform -n default --atomic -f <live values from helm get values>`. Only Grafana and Prometheus restart. Then `check` must report 0 problems.
2. **MUTATING, image promotion.**
   - `release_state.py values --release llm-agent --image coordinator=<new> --image feature-rag-specialist=<new> --git-sha $SHA --output promo.yaml`
   - `check --promotion promo.yaml`
   - `PROMOTION_APPROVED=yes … promote --execute`

Then apply the `g3/keda.yaml` overlay (transferHpa), and run A-05 and A-06 with `scripts/experiments/agent_experiment.sh` using typed A2A requests. Typed requests make no LLM call.

**Record:**
- request rate, task correctness and latency;
- trigger values, desired and available replicas, and scale events;
- CPU and RAM.

The kagent Agent Sandbox pilot does not count as scaling evidence.

**Rollback:**
- Serving: `autoscaling.enabled=false`, then the R1 `rollback`.
- Agents: delete ScaledObjects first, then `release_state` back to the rollback digests.

## Package 6: Istio gateway, phase 1 (internal)

- **Deploy:** P-A5 phase 1, with Service type ClusterIP. Routes: `features`, `fraud`, `a2a`, `agents` (kagent UI), `registry`, `grafana`.
- **Private:** MCP, agentgateway, KServe, Redis and Vault.
- **Check through a port-forward to the gateway Service:** `scripts/edge/negative_tests.py`, covering 401 without, with a wrong or a forged credential; 301 on HTTP; 429 on bursts; undeclared paths returning 404. Also check UI chat streaming with a synthetic prompt and the Registry UI.
- **Do not expose conversation history (R8-P1).** The kagent UI route stays behind Basic Auth and is not made public.

**Phase 2**, a public load balancer, domain and certificate, is a separate decision. The rubric names NGINX Ingress; Istio is the functional alternative and needs the instructor's confirmation.

**Rollback:** uninstall `ewallet-edge` and the gateway release.

## Package 7: later sessions

| Session | Content | Gate |
|---|---|---|
| B: ML A/B | KServe v1 control and v2 candidate; MLflow provenance; 90/10 then 50/50; rollback; dead candidate | v2 registered; no promotion without the owner |
| C: Vault | Official chart, Kubernetes auth, R2 roles (`bootstrap.json`), Fraud API patch (P7), unseal, rotation, negative tests | Vault unseal per wake; the Feature API's Vault is optional |
| D: Agent and LLM A/B | Planner variants (P9) on CPU; Qwen GPU variant (P10) | Synthetic prompts only; P10 needs GPU quota |
| Ansible (row 45) | After Session A: inventory, role and playbook deploying the synthetic transaction generator to one GCE VM as a systemd service, with an idempotent rerun | Separate approval; no VM in this batch |

**Recorded, not in this campaign:**
- Business Agent Sandbox, Substrate warm-up, Agent Fleet capacity;
- MCP service-level fallback;
- Airflow incremental materialization on cloud (C-A E1);
- streaming offline and online (C-A E2);
- Coordinator and streaming CD (P1);
- DP2 Gold validation, SCD2 and schema evidence;
- novel ideas, remaining screenshots, diagram redesign.

## Evidence

Each run goes under `docs/evidence/edai2/<area>/<run-id>/`, where area is `build`, `iam`, `cloud_lab`, `load`, `keda`, `edge`, `ml_ab`, `vault` or `llm_ab`.

Each run records:
- the rubric row from `RUBRIC_EVIDENCE_INDEX.md`;
- the source SHA, image digests and Helm revision;
- the exact command, start and end UTC, and the raw output;
- `kubectl get/top` snapshots;
- the verdict.

UI screenshots are native captures only. A row moves to CLOUD_VERIFIED only from these runs, and to COMPLETE only when every column-D deliverable exists.

## Results and final gates (2026-11-07)

### Package 1: executed

All three builds ran from `git archive` of `5801bcf77eb8a83c9e4e5cbd6e22d520fd0512ec` as `ewallet-cloudbuild`, with the pinned builder. Evidence: `docs/evidence/edai2/build/g3_20261107T142012Z`.

| Image | Build ID | Digest (deploy by this, never by tag) | Rollback digest |
|---|---|---|---|
| `fraud-api` | `0b965d93-ebb9-4688-8ef9-261d22e89dba` | `sha256:dbaf8b7060e6bebf477f2bff02dfede382102410b4ab68b51e836de9f7c1c803` | `sha256:20e9c2ad1272d1564198dc970dd402ead17dbca0b1881b3119dca0c4c90e3165` |
| `g3-byo-coordinator` | `e5a3f911-79b8-4077-be42-71a225e919d7` | `sha256:b84d8c7e3ddd83c792e853c61e9f5cfe5e4b6d6c917b8b06e5ad6a3bf4730bd4` | `sha256:483dd943290c02fdd4d0d3e9e38b4182dae76a01581e6e5e191ca977b7498aff` |
| `g3-byo-feature-rag-specialist` | `9623ea08-9e76-4ef9-9d12-7a054c3f956e` | `sha256:a88404fa8e54d28694cfe4e923f83b75e3904e5d2212ba484ee5d6ac6765f96e` | `sha256:5f848a1ce7942f4fd5027264bc54c3164d09065d3c8932fcd4a93bce8db57606` |

Each digest was re-hashed over the manifest bytes the registry serves. Each config carries `org.opencontainers.image.revision=5801bcf…`, and each tag is the full SHA. The SLSA provenance names the build ID and the source archive's sha256, `f533409401aa24d0…`.

**Trivy 0.75.0, HIGH and CRITICAL, remote:** no finding introduced compared with the deployed digests, and no secrets.
- The agent images fix 4 HIGH findings: 47 HIGH now, 51 before.
- `fraud-api` keeps its 3 existing CRITICAL findings:
  - `libsqlite3-0` CVE-2025-7458 and `zlib1g` CVE-2023-45853, both without a Debian fix;
  - `feast` 0.66.0 CVE-2026-18948, fixed in 0.67.0. Feast is pinned by the uv contract, so upgrading it is a separate decision.

**Known provenance gap:** `ai_platform/inference/Dockerfile` copies `uv` from `ghcr.io/astral-sh/uv:0.13.5` by tag, not by digest. The BYO Dockerfile pins it. Fix it in a later source change, then rebuild.

**Cost:** about 4.3 build-minutes, inside the free tier (gross about $0.03). About 0.5 GiB more registry storage and 183 MiB of source archives: under $0.06 a month.

### Package 2: final read-only validation

The plan was taken with the state lock on serial 10, and the lock was released afterwards. Its summary is in `docs/evidence/edai2/iam/r2_plan_20261107T142221Z`.

| Result | Value |
|---|---|
| Creates | 4, exactly the approved addresses |
| No-ops | 16 |
| Updates, deletions, replacements | 0 |
| GKE or network addresses | 0 |
| Guard | "plan is exactly the approved R2 change" |

**GCS access, measured.** `registry_mirror.pull()` was run against the real registry while every HTTP request was recorded. It made:
- `GET` on the object's metadata;
- `GET` on the object's media;
- one `GET` on bucket metadata.

The bucket call comes from `google-cloud-storage` 3.17.1's background `_bucket_metadata_cache`. When that call was forced to return 403, the mirror still succeeded. So the APIs need only `storage.objects.get` on the registry object. A conditional `objectViewer` on `features/registries/` grants exactly that. No `objects.list` and no `buckets.get` is required, and none is proposed.

**Workload Identity and Vault:**
- **KSA annotations.** `fraud-api` points to `ewallet-fraud-api@…` in `service-account.yaml.tmpl`; `feature-api` points to `featureApi.serviceAccount.gcpServiceAccount`.
- **Bindings.** Each `workloadIdentityUser` binding names `…svc.id.goog[ml-platform/<sa>]`.
- **Training identity.** `ewallet-training` keeps `ml-platform/training` and `kubeflow/pipeline-runner`. The second is outside the Terraform state, and neither is touched.
- **Vault is separate from GCP identity.** Vault uses the projected token with audience `vault` (Kubernetes auth through TokenReview); Workload Identity uses the GKE metadata server.

The Package 2 commands above are unchanged, and **still await approval**.

### Package 3: final preflight

| Check | Result |
|---|---|
| Node pool | Only `cpu-pool`, `e2-standard-4`, 0 nodes, autoscaling off; node auto-provisioning off |
| Quota | E2 CPUs 0 / 24 |
| PVCs | 15 Bound |
| Capacity on 5 nodes | 12,760m CPU and 48.5 GiB free after DaemonSets and 85% packing |
| Platform | 8,901m and 22.2 GiB |
| Experiments | Feature API ×3, Fraud API ×3, KEDA, gateway, agents ×3, in-cluster Locust: about 2,300m and 4.5 GiB |
| Total | About 88% of CPU and 55% of memory |
| `g3-llm` nodes | 3, holding 3,770m of 7,656m |

Gross list price per hour:

| Item | Asleep | 5 nodes |
|---|---|---|
| Nodes | — | $0.827 |
| Node IPs and boot disks | — | $0.063 |
| PVCs | $0.011 | $0.011 |
| Bucket and registry | $0.097 | $0.097 |
| **Total** | **$0.108** | **about $1.00** |
| Cluster fee | $0.10, normally offset by the GKE free tier for one zonal cluster | same |

Cloud Logging and Managed Prometheus are expected inside their free allotments. No load balancer is created before Package 6 phase 2.

**8 hours gross: about $8.0**, or $8.8 if the cluster fee is not offset. Free Trial credit applies to all of it.

**Budget.** `gcloud billing budgets list` did not answer, so the owner checks the remaining credit in the console before waking. A budget alert can be set there, which is an owner action.

**What `wake-full --nodes 5` changes:**
1. `gcloud container clusters resize ewallet-dev --node-pool cpu-pool --num-nodes=5`.
2. It waits for 5 Ready nodes.
3. It labels the first 3 nodes `ewallet.io/g3-llm=true`.
4. It scales every Deployment or StatefulSet whose live replicas differ from the sleep manifest: agents 2, `drift-mcp` 1 in both namespaces, `vllm-fallback` stays 0.

It runs no Helm, deletes nothing and starts no GPU. Its node-pool identity gate (one pool, autoscaling off, no auto-provisioning) runs only on `sleep`, so run it by hand first:

```bash
gcloud container node-pools list --cluster ewallet-dev --location asia-southeast1-b --format='value(name)'      # exactly: cpu-pool
gcloud container node-pools describe cpu-pool --cluster ewallet-dev --location asia-southeast1-b --format='value(autoscaling.enabled)'   # empty
gcloud container clusters describe ewallet-dev --location asia-southeast1-b --format='value(autoscaling.enableNodeAutoprovisioning)'      # empty
```

**Stop and sleep if** any of these fail, if Pods stay Pending, or if credit is low:

```bash
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py sleep --scale-pdb-guarded --execute --confirm-cluster ewallet-dev
```

### Package 4 and later, with the verified digests

```bash
FRAUD_IMAGE=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/fraud-api@sha256:dbaf8b7060e6bebf477f2bff02dfede382102410b4ab68b51e836de9f7c1c803
COORDINATOR_IMAGE=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/g3-byo-coordinator@sha256:b84d8c7e3ddd83c792e853c61e9f5cfe5e4b6d6c917b8b06e5ad6a3bf4730bd4
SPECIALIST_IMAGE=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/g3-byo-feature-rag-specialist@sha256:a88404fa8e54d28694cfe4e923f83b75e3904e5d2212ba484ee5d6ac6765f96e
SHA=5801bcf77eb8a83c9e4e5cbd6e22d520fd0512ec
```

- **Fraud API:** `fixed` mode, 1 replica, as Package 4.
- **Feature API:** `--set featureApi.image=$FRAUD_IMAGE`.
- **Agents:** `release_state.py values --image coordinator=$COORDINATOR_IMAGE --image feature-rag-specialist=$SPECIALIST_IMAGE --git-sha $SHA`, after the chart refresh.

No KEDA ownership transfer before the separate R1 approval. Agent tests use synthetic prompts only.

### Package 2: executed (2026-11-07)

Evidence: `docs/evidence/edai2/iam/r2_apply_20261107T143853Z`. Details are in [terraform_gcp_foundation.md](../../cloud/terraform_gcp_foundation.md#r2-định-danh-riêng-cho-serving-2026-11-07).

| Step | Tool | Result |
|---|---|---|
| Plan, lock on, serial 10 | Terraform | 4 to add, 0 change, 0 destroy; guard accepted |
| Apply | Terraform | The saved plan, checksum re-verified: 4 added |
| Workload Identity bindings | `gcloud add-iam-policy-binding` | 2 bindings added |
| Import | `terraform import`, lock on | 2 imports successful |
| Readback | Terraform, gcloud | Serial 13, exactly 6 new addresses; a later plan shows no changes |

Unchanged: `training`, `serving` and `generator` bindings, other bucket bindings, the node pool and the cluster.

### Package 3: final preflight (read-only)

Evidence: `docs/evidence/edai2/cloud_lab/wake_preflight_20261107T144329Z`.

| Check | Result |
|---|---|
| Identity gates | Only `cpu-pool`; autoscaling off; node auto-provisioning off |
| Nodes | 0 |
| E2 CPUs | 0 / 24 |
| In-use addresses | 0 / 8; 5 nodes need 5 |
| PVCs | 15 Bound |
| `release_state drift` | 0 problems for `llm-agent` and `llm-agent-mcp` |
| New image digests | All 3 present in Artifact Registry |

The `wake-full --nodes 5` dry run printed only PLAN lines: resize to 5, label 3 nodes, and restore exactly 5 Deployments (`drift-mcp` ×2, `drift-specialist`, `feature-rag-specialist`, `fraud-coordinator`). `vllm-fallback` and the system workloads recorded at 0 stay at 0.

**Capacity adjustment without a sixth node.** During KEDA scale-out arms, hold these at 0 to free 2,314m CPU and 6.1 GiB:
- `ewallet-ci`, Jenkins: 1,000m;
- `kubeflow`: 804m;
- `kagent-pilot`: 510m.

Use `kubectl scale --replicas=0` under the experiment package's approval, and restore the recorded counts afterwards.

**Billing.** Credit cannot be read from the API. The wake waits for the owner's console check.

**Budget for 8 hours:** about $8.0 gross, or $8.8 if the cluster fee is not offset by the free tier, all against Free Trial credit.

### G4.5: runtime stabilization after the GKE auto-upgrade (2026-11-07)

Evidence: `docs/evidence/edai2/cloud_lab/stabilize_20261107T173813Z`. The only mutations were 6 `CLUSTER MEET` commands and 6 synthetic Valkey keys, which were written and then deleted. Nothing else changed: no restarts, Helm, scaling, labels, PDBs or Terraform.

| Area | Result |
|---|---|
| GKE | Two operations, both DONE:<br>- `UPGRADE_MASTER` 15:58–16:06Z;<br>- `UPGRADE_NODES` 16:13–17:26Z: 5/5 nodes, 0 failed. PDB delay 3598 s, from `drift-mcp` (`minAvailable: 1`, 1 replica).<br>All 5 nodes are Ready on `1.36.4-gke.2377000`. `ewallet.io/g3-llm=true` is on exactly cbl0, jt0h and wcka, so no relabel was needed. |
| Valkey | Root cause E (A+B+C): every node had stale self and peer addresses.<br>Fixed with one 6-MEET sequence: all `myself` addresses are now correct, 16,384 slots, `--cluster check` OK, a `-c` client read and wrote in all 3 shards, and existing keys are readable.<br>The node IDs and the master/replica pairs did not change. One automatic in-pair failover: `1724…` is now master of 10923–16383. |
| Agent runtime | `ate-api-server` and `kagent-controller` became Ready through their own backoff.<br>Synthetic A2A: HTTP 200, `completed`, 2.57 s. Coordinator → Specialist → MCP gateway → `feature_lookup` (486 ms) and `fraud_scoring` (273 ms). Model version 1, probability 0.0639895560144887, trace `a6979d141c65491146a1d48aacd89aa7`. |
| Serving | Both APIs run `dbaf8b70…` under KSAs `fraud-api` and `feature-api` (own GSAs). `ewallet-serving` r1; predictor `cba90c22…`.<br>One lookup returned 200 in 0.28 s, with values identical to Package 4. One predict returned 200 in 0.39 s, probability 0.0639895560144887, correlation ID echoed. |
| Capacity | 19.6 CPU allocatable, 13.7 requested (70%), 5.9 free; memory 47% requested. cbl0 is at 98% CPU requests. |
| Still broken, out of scope | Kubeflow `persistenceagent` and `scheduledworkflow` are stuck in `Init:0/1` (network policies; no change made). |

**Maintenance policy (recommendation only, not applied).** The current state:
- REGULAR channel, node auto-upgrade on;
- `maintenancePolicy` is empty, so GKE may upgrade at any time;
- `1.37.1-gke.1552000` is already valid on REGULAR;
- Terraform's `modules/gke` has no `maintenance_policy`.

Recommendation:
1. **Recurring window**, every day 18:00–22:00 UTC (01:00–05:00 ICT), outside campaign hours.
2. **Campaign exclusion** with scope `no_minor_or_node_upgrades`, only for the campaign days and at most 14 days. Control-plane security patches still apply. Remove it when the campaign ends.

Neither cancels an operation already running; none is running now. Applying them by gcloud creates Terraform drift in `module.gke`. That is harmless while this state is never applied untargeted, but add a matching `maintenance_policy` block before any future full apply.

```bash
gcloud container clusters update ewallet-dev --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26 \
  --maintenance-window-start=2026-11-08T18:00:00Z --maintenance-window-end=2026-11-08T22:00:00Z \
  --maintenance-window-recurrence='FREQ=DAILY'
gcloud container clusters update ewallet-dev --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26 \
  --add-maintenance-exclusion-name=edai2-campaign --add-maintenance-exclusion-start=<start> \
  --add-maintenance-exclusion-end=<start + ≤14 days> --add-maintenance-exclusion-scope=no_minor_or_node_upgrades
# Rollback
gcloud container clusters update ewallet-dev --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26 --remove-maintenance-exclusion=edai2-campaign
gcloud container clusters update ewallet-dev --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26 --clear-maintenance-window
```

**Package 5 readiness.**

| Package | State | What it still needs |
|---|---|---|
| 5-A Locust (P-A2) | Ready | Use the Package 4 `fixture/verified.jsonl`: 500/500 verified against the online window. The raw `split=test` snapshot falls outside the Feast window. One 1-minute smoke run per target first. |
| 5-B serving KEDA (P-A3) | Ready after 5-A | KEDA is not installed: 0 CRDs, no `keda` namespace. The thresholds come from the 5-A fixed arms. The `helm upgrade llm-agent` scrape-job step also restarts Grafana and Prometheus (checksum drift). The R1 handover is required. |
| 5-C agent KEDA (P-A4) | Needs approvals | The agents still run r41 images (`483dd943…`, `5f848a1c…`), not `b84d8c7e…` / `a88404fa…`. First the chart refresh, then image promotion, then the `transferHpa` overlay; 3 HPAs (2/3 replicas) exist today. Synthetic prompts only (R8-P1). |
