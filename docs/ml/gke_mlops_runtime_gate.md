# Bounded GKE MLOps runtime gate

This runbook deploys only the bounded Gate 2 runtime: PostgreSQL, MLflow and
standalone Kubeflow Pipelines 2.17.3. It does not create a training snapshot,
submit the compiled pipeline, train/register/promote a model, deploy Feast
Online/KServe, or run Spark.

## Frozen topology

```text
immutable GCS snapshot ──> KFP 2.17.3 task pods ──> MLflow ClusterIP
          │                         │                    │
          │                         └── Workload Identity┤
          └──────────────────────────── GCS artifacts <──┘
                                                       │
                                         PostgreSQL ClusterIP + PVC
```

Artifact Registry holds two explicit images. Kubernetes DNS connects KFP to
`mlflow.ml-platform.svc.cluster.local:5000` and MLflow to
`mlflow-postgresql.ml-platform.svc.cluster.local:5432`. Neither service has a
LoadBalancer or public ingress; administrative access uses port-forwarding.
Production model promotion remains manual and is outside this gate.

## Prerequisites and configuration

- Existing project `ewallet-data-ai-platform-hg26`, zonal cluster `ewallet-dev`
  and node pool `cpu-pool`.
- Existing Artifact Registry repository `ewallet-platform` and GCS data bucket.
- Existing GSA
  `ewallet-training@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com` with
  the previously approved GCS permissions.
- `git`, `docker`, `gcloud`, `kubectl`, `gke-gcloud-auth-plugin`, `curl`,
  `openssl` and `sha256sum`. On Debian/Ubuntu, install the required GKE IAM
  authentication plugin with
  `sudo apt-get install google-cloud-cli-gke-gcloud-auth-plugin`.
- Docker must have enough local disk for both bounded images.

Start in this worktree and load only non-secret settings:

```bash
cd ~/Develop/ewallet-data-ai-platform
set -a
source configs/mlops/gke-runtime.env.example
set +a
```

The scripts are dry-run by default. Every local or cloud mutation requires
`--execute`. Review the dry-run output before repeating a command with that
flag. The only runtime secret is supplied in the current shell and never
written to the repository:

```bash
export MLFLOW_DB_PASSWORD="$(openssl rand -hex 24)"
```

## DNS control-plane access contract

The DNS-based GKE endpoint is the canonical operator access path. An operator
can be behind dynamic NAT, CGNAT or a multi-egress network where consecutive
requests use different public addresses, so repeatedly allowlisting a sampled
client address as `/32` is not reliable. Keep the existing IP authorized-network
policy unchanged; do not disable authorized networks, open `0.0.0.0/0`, or add
each transient client address.

DNS access is a one-time cluster prerequisite and must be enabled explicitly by
an authorized operator if it is absent:

```bash
gcloud container clusters update "$GKE_CLUSTER" \
  --zone "$GKE_ZONE" \
  --project "$GCP_PROJECT_ID" \
  --enable-dns-access
```

Do not add `--enable-k8s-tokens-via-dns` or
`--enable-k8s-certs-via-dns`. Authentication remains IAM-based through
`gke-gcloud-auth-plugin`; Kubernetes ServiceAccount bearer tokens and client
certificates over DNS remain disabled. The connection command is always:

```bash
gcloud container clusters get-credentials "$GKE_CLUSTER" \
  --zone "$GKE_ZONE" \
  --project "$GCP_PROJECT_ID" \
  --dns-endpoint
```

The DNS API path and project preflight have been verified against the existing
cluster. No image has been built or pushed and PostgreSQL, MLflow and KFP have
not been deployed.

## Execution order

### 1. Contract preflight

This verifies the committed pipeline SHA-256, its single exact runtime image,
the pinned images, KFP version, GCS artifact root and canonical identities.

```bash
scripts/mlops/preflight.sh
```

Expected: `Local contract checks: PASS` followed by read-only cloud commands.

### 2. Build and push immutable runtime images

The F9 build uses repository-root context because its Dockerfile copies
`ai_platform/ml/pyproject.toml`, the committed `uv.lock`, and application
source. Both Dockerfiles install with `uv sync --frozen --no-dev`.

```bash
scripts/mlops/build_image.sh f9
scripts/mlops/build_image.sh mlflow
scripts/mlops/push_image.sh f9
scripts/mlops/push_image.sh mlflow

scripts/mlops/build_image.sh f9 --execute
scripts/mlops/build_image.sh mlflow --execute
scripts/mlops/push_image.sh f9 --execute
scripts/mlops/push_image.sh mlflow --execute
```

The F9 operation fails unless the requested image exactly matches every image
bound into `f9_canonical_pipeline.yaml`. `latest` is rejected. Push uses a
short-lived `gcloud` token and never stores registry credentials in source.

### 3. Start the bounded node and select the DNS cluster endpoint

```bash
scripts/mlops/cluster_capacity.sh up
scripts/mlops/connect_cluster.sh

scripts/mlops/cluster_capacity.sh up --execute
scripts/mlops/connect_cluster.sh --execute
scripts/mlops/preflight.sh --execute
```

`connect_cluster.sh` reads and validates the cluster configuration before
writing kubeconfig, then calls `get-credentials --dns-endpoint`; it never falls
back to an IP endpoint. Expected: `cpu-pool` has one node, the Kubernetes
context names `ewallet-dev`, the active API server exactly matches the
discovered cluster DNS endpoint and ends in `.gke.goog`, and the read-only
preflight prints `PASS`.

### 4. Prepare identities and bind Workload Identity

The bind command first idempotently applies the canonical `ml-platform` and
`kubeflow` Namespace manifests and the canonical `training` and
`pipeline-runner` ServiceAccount manifests. Only after those prerequisites
exist does it add the two GSA IAM members and annotate both KSAs. Its dry-run
prints that exact order.

```bash
scripts/mlops/workload_identity.sh bind
scripts/mlops/workload_identity.sh bind --execute
```

The operation grants only `roles/iam.workloadIdentityUser` from the canonical
training GSA to `ml-platform/training` and `kubeflow/pipeline-runner`. It is
safe to repeat: Kubernetes resources use `apply`, IAM policy additions are
idempotent, and annotations use `--overwrite`. No credential file is created.

### 5. Deploy PostgreSQL and MLflow

```bash
scripts/mlops/deploy_mlflow.sh
scripts/mlops/deploy_mlflow.sh --execute
```

The script creates two Kubernetes Secrets from the shell at execution time:
database credentials and the PostgreSQL backend URI. It creates a ConfigMap
for the GCS artifact destination. PostgreSQL uses an internal Service, one
StatefulSet replica, a 5 GiB PVC, 100m/256 MiB requests and 500m/768 MiB
limits. MLflow uses one Deployment replica and one server worker, an internal
Service, 250m/1 GiB requests and 1 CPU/2 GiB limits, plus `/health` readiness
and liveness probes. The bounded PostgreSQL, MLflow and planned KFP runtime
requests 4,522 MiB in total; the previous 4,010 MiB estimate increases by 512
MiB solely because of the corrected MLflow memory request.

### 6. Deploy pinned standalone KFP

```bash
scripts/mlops/deploy_kfp.sh
scripts/mlops/deploy_kfp.sh --execute
```

The only upstream sources are the two Kubeflow Pipelines kustomizations pinned
with `ref=2.17.3`; the workflow does not use `curl | kubectl`, Helm or full
Kubeflow. The local `infra/kubernetes/kfp` overlay consumes the pinned
`env/dev` source, preserves its single `pipeline-runner` ServiceAccount and
declaratively excludes the inverse-proxy ServiceAccount, Role, RoleBinding,
ConfigMap and Deployment. This keeps the namespace at PodSecurity `baseline`
and leaves `ml-pipeline-ui` available only through its ClusterIP Service and a
local port-forward. Reapplying the deploy script also deletes exactly the stale
inverse-proxy ServiceAccount, Role, RoleBinding, ConfigMap and Deployment with
explicit names and `--ignore-not-found`, then fails closed unless all five are
absent. It does not use pruning or selectors and does not delete the namespace
or any other KFP resource.

The pinned upstream UI probes use `/apis/v1beta1/healthz` on port 3000. The
canonical overlay preserves those probes: a 404 while the backend control
plane is incomplete does not independently prove that the upstream probe path
is wrong.

Although the upstream source is pinned to `ref=2.17.3`, its development
overlay replaces the release images with mutable `master` tags. The canonical
overlay therefore pins the complete KFP runtime to the official 2.17.3 OCI
index digests. Registry metadata for every digest identifies release commit
`a0733cb51b0484a6050cafee989ffe3312f808ad` and version `2.17.3`:

| Runtime image | Immutable release digest |
| --- | --- |
| `kfp-api-server` | `sha256:1e03770233cadc146df0b83a82603b93f32fc3b57bd6dcb29375e2e8d28eb605` |
| `kfp-cache-deployer` | `sha256:97784329a5af2274097d13095ddc71f8e0c72ccad76fc4e67d40a06eb14417e7` |
| `kfp-cache-server` | `sha256:09f16342f896b05714933661fb65bf49b4faa5f07f2414002f1e015bc8a608e0` |
| `kfp-frontend` | `sha256:06d5481f404f89647ac5ee958f6665446014fdd78e6c1b195a1687ff1badf1d8` |
| `kfp-metadata-envoy` | `sha256:5cc007cfa3ec2f3f9442430a66d4e65239760cae2ff66dfe576e1b2f685e0cfb` |
| `kfp-metadata-writer` | `sha256:fbcf3704239ee92135a7f7c82ba177c65977ab1da7044b4cc02b6de5ace698bb` |
| `kfp-persistence-agent` | `sha256:c0b417e60009d058c2d342ab33424e10cf57121d9b771cca9c09dda32475c03e` |
| `kfp-scheduled-workflow-controller` | `sha256:e2e5b80707955f3295e8a24c340c838f81398b988a9d73318918bea5ffdde7d8` |
| `kfp-viewer-crd-controller` | `sha256:2b6e0e92b99f709c11a484fbf4163fc519003d22c4f9d040f3fba862f5b69780` |
| `kfp-visualization-server` | `sha256:d143f368b428c92cccf2b33c4c1b0274f8b1a14cf04f25659fe0f091e96cef6f` |
| `kfp-driver` (`V2_DRIVER_IMAGE`) | `sha256:4461e2f63739a80810405fdf76bd4727ea68561a54b51e4bbf869b12d7a8d163` |
| `kfp-launcher` (`V2_LAUNCHER_IMAGE`) | `sha256:4f1c3cff2725413cbb903f623aed074c52e3c70c355501c87b29db044c83fcd2` |

All twelve indexes publish `linux/amd64`; all except metadata writer also
publish `linux/arm64`. The bounded `e2-standard-4` node pool is amd64, so this
set covers the canonical runtime. Do not change the API probe to match a newer
development binary: the release 2.17.3 API server supports the preserved
`/apis/v1beta1/healthz` probe.

The previously deployed `master` API server reached MySQL AutoMigrate before
its incompatible startup probe restarted the Pod. Backward schema
compatibility with 2.17.3 has not been established. Before an authorized
reapply, retain the current resource descriptions and API/MySQL logs as
evidence and arrange a separately reviewed, non-destructive database backup
if the metadata must be retained. Do not delete, reset, recreate or manually
migrate MySQL. If the pinned API server reports a schema or migration error,
stop recovery and preserve the database for investigation.

The committed compiled YAML is checksum-validated but is neither regenerated
nor uploaded by this gate. Both deployment scripts may reapply their canonical
Namespace and ServiceAccount manifests. `kubectl apply` does not remove the
Workload Identity annotation added separately with `kubectl annotate`, so the
identity binding survives deployment reapplication.

### 7. Validate rollouts, identities and internal health

```bash
mkdir -p tmp
scripts/mlops/validate_rollouts.sh --execute | tee tmp/mlops-rollouts.txt
scripts/mlops/smoke_mlflow.sh --execute | tee tmp/mlflow-smoke.txt
scripts/mlops/health_kfp.sh --execute | tee tmp/kfp-health.txt
```

Expected results are available PostgreSQL/MLflow/KFP deployments,
`pg_isready` success, exact GSA annotations on both KSAs, both expected
`roles/iam.workloadIdentityUser` members on the GSA, MLflow `/health` success,
and successful KFP API/UI HTTP checks through temporary local port-forwards.

### 8. Collect evidence

```bash
scripts/mlops/collect_evidence.sh
scripts/mlops/collect_evidence.sh --execute
```

Retain the generated directory under `tmp/mlops-gke-evidence/`, the three
validation transcripts above, Artifact Registry digests, pipeline SHA-256,
Git commit, resource listings, Workload Identity evidence, descriptions and
bounded log tails. Capture MLflow and KFP UI screenshots through temporary
port-forwards if presentation evidence is required.

## Cleanup and rollback

Review and export evidence first, remove only this bounded runtime, then return
the existing node pool to zero. The valid cleanup interface is
`scripts/mlops/cleanup.sh <mlflow|kfp|all>`; add `--execute` only after
reviewing its dry-run:

```bash
scripts/mlops/cleanup.sh all
scripts/mlops/cluster_capacity.sh down

export ALLOW_KFP_CONTROL_PLANE_DELETE=YES
scripts/mlops/cleanup.sh all --execute
scripts/mlops/cluster_capacity.sh down --execute
unset ALLOW_KFP_CONTROL_PLANE_DELETE MLFLOW_DB_PASSWORD
```

The PostgreSQL PVC is preserved by default. Delete it only after evidence and
metadata retention review:

```bash
scripts/mlops/cleanup.sh mlflow --delete-postgres-data
scripts/mlops/cleanup.sh mlflow --delete-postgres-data --execute
```

Cleanup never deletes or modifies the training GSA or its IAM policy, the GKE
cluster, GCS buckets/objects, Artifact Registry repositories/images, canonical
datasets, DP1/DP2/DP3 outputs, MLflow GCS artifacts, or production aliases.
The KSA objects can be recreated and rebound idempotently by the identity
preparation step. Do not set
`ALLOW_KFP_CONTROL_PLANE_DELETE=YES` unless the `kubeflow` namespace is
dedicated to this standalone pilot.

## Cost control and stop conditions

- Keep `cpu-pool` at one node only during deployment verification, then return
  it to zero.
- Do not run the compiled KFP pipeline during this gate.
- Stop before execution if image/SHA binding, project, GSA, cluster, namespace,
  GCS root or KFP version differs from this runbook.
- Stop on any IAM/quota/cluster ownership ambiguity; do not create substitute
  infrastructure.
