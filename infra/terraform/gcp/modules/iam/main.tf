locals {
  service_accounts = {
    generator = "Writes deterministic source shards and manifests"
    spark     = "Runs Serverless for Apache Spark data jobs"
    training  = "Reads features and writes training and ML artifacts"
    serving   = "Reads approved model artifacts for inference"
    # R2: one identity per serving API, so neither borrows training's bucket-wide access.
    fraud_api   = "Fraud API: reads the online Feast registry only"
    feature_api = "Feature Retrieval API: reads the online Feast registry only"
    gke_nodes   = "Runs GKE node system and workload pods"
    ci          = "Builds immutable images and deploys reviewed releases"
  }
}

resource "google_service_account" "workload" {
  for_each = local.service_accounts

  project      = var.project_id
  account_id   = "ewallet-${replace(each.key, "_", "-")}"
  display_name = "E-Wallet ${each.key}"
  description  = each.value
}

locals {
  data_bucket_roles = {
    generator = "roles/storage.objectUser"
    spark     = "roles/storage.objectUser"
    training  = "roles/storage.objectUser"
    serving   = "roles/storage.objectViewer"
  }
  temporary_bucket_roles = {
    spark    = "roles/storage.objectUser"
    training = "roles/storage.objectUser"
  }
  gke_project_roles = toset([
    "roles/artifactregistry.reader",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
  ])
  ci_project_roles = toset([
    "roles/artifactregistry.writer",
    "roles/container.developer",
    "roles/logging.viewer",
  ])
  spark_project_roles = toset([
    "roles/dataproc.worker",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])
}

resource "google_storage_bucket_iam_member" "canonical" {
  for_each = local.data_bucket_roles

  bucket = var.data_bucket_name
  role   = each.value
  member = "serviceAccount:${google_service_account.workload[each.key].email}"
}

locals {
  # The serving APIs read one object, the online Feast registry, with objects.get.
  # The condition keeps them out of the rest of the 3.4 TiB data bucket.
  registry_readers = toset(["fraud_api", "feature_api"])
  registry_prefix  = "features/registries/"
}

resource "google_storage_bucket_iam_member" "registry_reader" {
  for_each = local.registry_readers

  bucket = var.data_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.workload[each.key].email}"

  condition {
    title       = "online-feast-registry-only"
    description = "Read objects under ${local.registry_prefix} and nothing else"
    expression  = "resource.name.startsWith(\"projects/_/buckets/${var.data_bucket_name}/objects/${local.registry_prefix}\")"
  }
}

resource "google_storage_bucket_iam_member" "temporary" {
  for_each = local.temporary_bucket_roles

  bucket = var.temporary_bucket_name
  role   = each.value
  member = "serviceAccount:${google_service_account.workload[each.key].email}"
}

resource "google_project_iam_member" "gke_nodes" {
  for_each = local.gke_project_roles

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.workload["gke_nodes"].email}"
}

resource "google_project_iam_member" "ci" {
  for_each = local.ci_project_roles

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.workload["ci"].email}"
}

resource "google_project_iam_member" "spark" {
  for_each = local.spark_project_roles

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.workload["spark"].email}"
}

resource "google_service_account_iam_member" "ci_can_submit_dataproc" {
  service_account_id = google_service_account.workload["spark"].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.workload["ci"].email}"
}

resource "google_service_account_iam_member" "operator_can_submit_dataproc" {
  for_each = var.dataproc_submitter_members

  service_account_id = google_service_account.workload["spark"].name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}
