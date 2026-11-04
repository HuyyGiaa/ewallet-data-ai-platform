# R2 Terraform Foundation Implementation Plan

> **Historical plan/spec — not current source of truth.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision the reproducible, cost-bounded GCP foundation needed by the E-Wallet cloud data, ML and Kubernetes workloads without creating billable compute by default.

**Architecture:** Terraform uses a one-time GCS state bootstrap followed by a modular `dev` stack. The first safe apply creates APIs, private GCS buckets, IAM identities, Artifact Registry and a VPC; Dataproc is serverless and GKE/Jenkins are implemented behind explicit opt-in flags so a routine apply cannot start persistent compute.

**Tech Stack:** Terraform >= 1.6, HashiCorp Google provider 7.x, GCS, IAM, Artifact Registry, custom-mode VPC, Dataproc Serverless prerequisites, GKE Standard, Compute Engine.

**Spec:** `docs/superpowers/specs/2026-10-21-cloud-platform-refactor-design.md`

## Global Constraints

- GCP project is `ewallet-data-ai-platform-hg26`, region `asia-southeast1`, zone `asia-southeast1-b`.
- Terraform must not manage, delete or loosen the existing billing budget and alerts.
- Buckets use uniform bucket-level access and enforced public-access prevention.
- Canonical data is protected from accidental Terraform deletion; only temporary/staging objects have automatic expiry.
- IAM is split by workload identity; no service-account JSON key is created or committed.
- Dataproc uses serverless batches; no persistent Dataproc cluster is provisioned.
- GKE and Jenkins are disabled by default and require explicit variables before Terraform can create billable compute.
- `terraform.tfstate`, `.terraform/`, plan binaries, variable secrets and credentials are never committed.
- Terraform must produce a reviewed plan before every apply; destructive changes require a separate review.
- The R2 implementation must not run the 100 GB generator, Spark pipeline, Helm, ML training or serving.

## Review Focus

- Missing or wrong GCP project ID must fail variable validation before a plan can target another project.
- Default variables must plan zero GKE clusters, node pools and Jenkins VMs.
- Canonical and Terraform-state buckets must reject destructive deletion through lifecycle/deletion protection.
- Temporary objects must have bounded retention without applying expiry to canonical data.
- IAM tests must reject primitive project roles (`owner`, `editor`) and service-account key resources.

---

### Task 1: Terraform repository safety contracts

**Files:**
- Modify: `.gitignore`
- Create: `tests/contract/test_terraform_foundation.py`

**Interfaces:**
- Consumes: repository root and the approved cloud design spec.
- Produces: static contracts for ignored Terraform runtime files, module layout, cost gates, bucket safety and IAM safety.

- [ ] **Step 1: Write contract tests that assert the exact R2 layout and safety rules**

  Assert ignored state/plan files; exact project validation; `enable_gke=false`; `enable_jenkins_vm=false`; protected canonical/state buckets; temporary lifecycle; no primitive roles or `google_service_account_key` resources.

- [ ] **Step 2: Run the contract suite and verify it fails because Terraform is absent**

  Run: `python -m unittest tests.contract.test_terraform_foundation -v`

- [ ] **Step 3: Add Terraform runtime patterns to `.gitignore`**

  Ignore `.terraform/`, `*.tfstate*`, `*.tfplan`, `.terraform.tfstate.lock.info`, crash logs and non-example `.tfvars`; keep `.terraform.lock.hcl` tracked.

- [ ] **Step 4: Run the contract suite and confirm only implementation assertions remain failing**

### Task 2: Remote state bootstrap

**Files:**
- Create: `infra/terraform/gcp/bootstrap/versions.tf`
- Create: `infra/terraform/gcp/bootstrap/variables.tf`
- Create: `infra/terraform/gcp/bootstrap/main.tf`
- Create: `infra/terraform/gcp/bootstrap/outputs.tf`
- Create: `infra/terraform/gcp/bootstrap/terraform.tfvars.example`

**Interfaces:**
- Consumes: project ID, region and globally unique state bucket name.
- Produces: a private, versioned GCS state bucket name for the `dev` backend.

- [ ] **Step 1: Define Terraform/provider constraints and validated bootstrap inputs**

- [ ] **Step 2: Create the protected state bucket**

  Enable uniform access, public-access prevention, versioning, noncurrent-version cleanup and deletion prevention. Do not create credentials.

- [ ] **Step 3: Export the state bucket name**

- [ ] **Step 4: Run `terraform fmt -check`, `terraform init -backend=false` and `terraform validate` in bootstrap**

### Task 3: Project services, network and storage modules

**Files:**
- Create: `infra/terraform/gcp/modules/project_services/{variables,main,outputs}.tf`
- Create: `infra/terraform/gcp/modules/network/{variables,main,outputs}.tf`
- Create: `infra/terraform/gcp/modules/storage/{variables,main,outputs}.tf`

**Interfaces:**
- Consumes: project ID, region, subnet CIDR, canonical and temporary bucket names.
- Produces: enabled APIs, custom VPC/subnet with Private Google Access, protected canonical bucket and expiring temporary bucket.

- [ ] **Step 1: Implement required API enablement with dependent-service protection**

- [ ] **Step 2: Implement a custom-mode VPC and regional subnet**

  Enable Private Google Access and allow only internal subnet traffic; do not create a public ingress rule.

- [ ] **Step 3: Implement storage safety boundaries**

  Canonical bucket has no automatic data expiry and cannot be destroyed with objects. Temporary bucket expires objects after seven days and aborts incomplete multipart uploads after one day.

- [ ] **Step 4: Export resource identifiers consumed by later modules**

### Task 4: Artifact Registry and least-privilege workload IAM

**Files:**
- Create: `infra/terraform/gcp/modules/artifact_registry/{variables,main,outputs}.tf`
- Create: `infra/terraform/gcp/modules/iam/{variables,main,outputs}.tf`

**Interfaces:**
- Consumes: project ID, region and bucket names.
- Produces: Docker repository plus generator, Spark, training, serving and CI service-account emails.

- [ ] **Step 1: Create one regional Docker Artifact Registry repository**

- [ ] **Step 2: Create workload service accounts without keys**

- [ ] **Step 3: Grant bucket roles by responsibility**

  Generator writes source/manifests, Spark reads/writes pipeline data, training reads features/training and writes ML artifacts, serving reads model artifacts, and CI reads/writes Artifact Registry. Use predefined narrow roles only.

- [ ] **Step 4: Grant Dataproc worker and service-account-user roles to the Spark identity**

- [ ] **Step 5: Export repository URL and service-account emails**

### Task 5: Cost-gated GKE and Jenkins modules

**Files:**
- Create: `infra/terraform/gcp/modules/gke/{variables,main,outputs}.tf`
- Create: `infra/terraform/gcp/modules/jenkins_vm/{variables,main,outputs}.tf`

**Interfaces:**
- Consumes: explicit enable flags, network/subnet, zone, workload identities and machine-size limits.
- Produces: optional GKE cluster/node pool and optional Jenkins VM; empty/null outputs when disabled.

- [ ] **Step 1: Implement a zonal GKE Standard cluster behind `enabled`**

  Use Workload Identity, release channel, Shielded Nodes, no legacy client certificate and a separately managed CPU node pool. Autoscaling bounds and machine type are variables with small defaults; no GPU.

- [ ] **Step 2: Implement a Jenkins VM behind `enabled`**

  Use OS Login, Shielded VM, a dedicated service account, balanced disk and no embedded SSH key or startup secret.

- [ ] **Step 3: Assert disabled defaults produce no persistent compute resources**

### Task 6: Compose the development environment

**Files:**
- Create: `infra/terraform/gcp/environments/dev/versions.tf`
- Create: `infra/terraform/gcp/environments/dev/providers.tf`
- Create: `infra/terraform/gcp/environments/dev/backend.tf.example`
- Create: `infra/terraform/gcp/environments/dev/variables.tf`
- Create: `infra/terraform/gcp/environments/dev/main.tf`
- Create: `infra/terraform/gcp/environments/dev/outputs.tf`
- Create: `infra/terraform/gcp/environments/dev/terraform.tfvars.example`

**Interfaces:**
- Consumes: module interfaces from Tasks 3-5.
- Produces: stable outputs for runtime config, image build, Dataproc submission, Helm and Ansible.

- [ ] **Step 1: Define validated environment inputs and safe defaults**

- [ ] **Step 2: Wire modules in dependency order**

- [ ] **Step 3: Export data/temp bucket, network/subnet, registry, service accounts, optional cluster and Jenkins endpoints**

- [ ] **Step 4: Initialize with `-backend=false`, validate and create a saved plan with compute disabled**

  The plan must be inspected for zero GKE cluster/node-pool and zero Jenkins VM instances before apply.

### Task 7: Reproducibility, cost and cleanup documentation

**Files:**
- Create: `docs/cloud/terraform_gcp_foundation.md`
- Create: `ops/validation/validate_terraform_foundation.sh`
- Modify: `README.md`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes: bootstrap and dev Terraform commands/outputs.
- Produces: exact operator runbook for bootstrap, backend migration, safe plan, optional compute activation, scale-down and destroy review.

- [ ] **Step 1: Document ADC versus runtime identities and why no key file is needed**

- [ ] **Step 2: Document two-stage state bootstrap and backend migration**

- [ ] **Step 3: Document cost gates and plan inspection**

  Explain that GCS/API/IAM/registry/VPC are the initial apply; GKE/Jenkins remain opt-in; Dataproc incurs compute cost only when a batch runs.

- [ ] **Step 4: Document outputs to environment variables**

- [ ] **Step 5: Add an executable validation wrapper and docs index links**

### Task 8: Final static validation and reviewed plan checkpoint

**Files:**
- Create after a successful real plan: `docs/evidence/cloud/r2_terraform_plan.json`

**Interfaces:**
- Consumes: formatted/validated Terraform and a user-observed GCP plan.
- Produces: machine-readable R2 evidence without credential, state or developer paths.

- [ ] **Step 1: Run all Terraform format/validate and repository contract tests**

- [ ] **Step 2: Run secret/path scan and `git diff --check`**

- [ ] **Step 3: Generate and review the no-compute Terraform plan**

  Stop before `terraform apply`. Applying is the user-observed cost checkpoint.

- [ ] **Step 4: Record only non-secret plan counts and selected safety settings as evidence**

- [ ] **Step 5: Commit verified R2 source/docs/evidence explicitly; do not push**
