locals {
  workload_identity_ksa = {
    generator = "data-platform/generator"
    training  = "ml-platform/training"
    serving   = "serving/fraud-inference"
  }
}

resource "google_service_account_iam_member" "workload_identity" {
  for_each = var.enabled ? local.workload_identity_ksa : {}

  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.service_account_emails[each.key]}"
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${each.value}]"
}
