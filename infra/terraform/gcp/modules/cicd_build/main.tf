# Least-privilege identities for the in-cluster Jenkins controller.
#
# ewallet-jenkins: bound by Workload Identity to KSA ewallet-ci/jenkins. It can
#   only submit/read Cloud Build builds, stage sources in one bucket and act as
#   the build runner. It cannot push images or touch any other bucket.
# ewallet-cloudbuild: the identity Cloud Build steps run as. It can only write
#   images to one Artifact Registry repository and read/write the build bucket.
# Kubernetes deployment rights are namespace-scoped RBAC
# (infra/kubernetes/jenkins/cd-rbac.yaml), never project IAM.

locals {
  jenkins_ksa = "${var.project_id}.svc.id.goog[${var.jenkins_namespace}/${var.jenkins_service_account}]"
}

resource "google_service_account" "jenkins" {
  project      = var.project_id
  account_id   = "ewallet-jenkins"
  display_name = "E-Wallet in-cluster Jenkins"
  description  = "Submits Cloud Build jobs for reviewed source snapshots; no deploy or push rights"
}

resource "google_service_account" "cloudbuild" {
  project      = var.project_id
  account_id   = "ewallet-cloudbuild"
  display_name = "E-Wallet Cloud Build runner"
  description  = "Runs Cloud Build steps and publishes immutable images to one repository"
}

resource "google_storage_bucket" "builds" {
  project                     = var.project_id
  name                        = "${var.project_id}-cicd-builds"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  lifecycle_rule {
    condition { age = var.build_object_retention_days }
    action { type = "Delete" }
  }
}

# Build logs live apart from staged sources: Cloud Build composes and deletes
# its "-appending-" log chunks, so the runner needs objectAdmin here, while the
# source bucket stays create-only (run 3 of edai2-cd-rag-pipeline failed with
# LOGGING_FAILURE when logs shared the source bucket).
resource "google_storage_bucket" "build_logs" {
  project                     = var.project_id
  name                        = "${var.project_id}-cicd-build-logs"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  lifecycle_rule {
    condition { age = var.build_object_retention_days }
    action { type = "Delete" }
  }
}

resource "google_storage_bucket_iam_member" "runner_writes_logs" {
  for_each = toset(["roles/storage.objectAdmin", "roles/storage.legacyBucketReader"])

  bucket = google_storage_bucket.build_logs.name
  role   = each.value
  member = "serviceAccount:${google_service_account.cloudbuild.email}"
}

resource "google_storage_bucket_iam_member" "jenkins_reads_logs" {
  bucket = google_storage_bucket.build_logs.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.jenkins.email}"
}

resource "google_service_account_iam_member" "jenkins_workload_identity" {
  service_account_id = google_service_account.jenkins.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${local.jenkins_ksa}"
}

# Narrower than roles/cloudbuild.builds.editor (which adds builds.list,
# locations.*, operations.*, resourcemanager.projects.* and RBE blobs). Cloud
# Build has no resource-level IAM, so the binding is project-level, but a build
# can only run as a service account Jenkins may actAs: ewallet-cloudbuild. The
# project default build identity (Compute default SA) needs actAs and is denied.
resource "google_project_iam_custom_role" "build_submitter" {
  project     = var.project_id
  role_id     = "ewalletCicdBuildSubmitter"
  title       = "E-Wallet CI/CD build submitter"
  description = "Create, read and cancel Cloud Build builds; nothing else"
  permissions = [
    "cloudbuild.builds.create",
    "cloudbuild.builds.get",
    "cloudbuild.builds.update",
  ]
}

resource "google_project_iam_member" "jenkins_submits_builds" {
  project = var.project_id
  role    = google_project_iam_custom_role.build_submitter.id
  member  = "serviceAccount:${google_service_account.jenkins.email}"
}

resource "google_service_account_iam_member" "jenkins_acts_as_runner" {
  service_account_id = google_service_account.cloudbuild.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.jenkins.email}"
}

resource "google_storage_bucket_iam_member" "jenkins_stages_sources" {
  for_each = toset(["roles/storage.objectCreator", "roles/storage.objectViewer"])

  bucket = google_storage_bucket.builds.name
  role   = each.value
  member = "serviceAccount:${google_service_account.jenkins.email}"
}

# Cloud Build validates a user-provided logsBucket with storage.buckets.get
# (run 1 of edai2-cd-rag-pipeline failed FAILED_PRECONDITION without it);
# legacyBucketReader adds only buckets.get and objects.list on this bucket.
resource "google_storage_bucket_iam_member" "runner_reads_sources_writes_logs" {
  for_each = toset(["roles/storage.objectCreator", "roles/storage.objectViewer", "roles/storage.legacyBucketReader"])

  bucket = google_storage_bucket.builds.name
  role   = each.value
  member = "serviceAccount:${google_service_account.cloudbuild.email}"
}

resource "google_artifact_registry_repository_iam_member" "runner_pushes_images" {
  project    = var.project_id
  location   = var.region
  repository = var.artifact_repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.cloudbuild.email}"
}
