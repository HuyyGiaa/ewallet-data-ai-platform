locals {
  batch_runtime = {
    project_id            = var.project_id
    region                = var.region
    subnet                = var.subnet_self_link
    service_account_email = var.service_account_email
    staging_bucket        = var.temporary_bucket_name
    execution_mode        = "serverless-batch"
    performance_tier      = "standard"
  }
}

resource "terraform_data" "batch_prerequisites" {
  input = local.batch_runtime

  lifecycle {
    precondition {
      condition     = endswith(var.service_account_email, ".gserviceaccount.com")
      error_message = "service_account_email must be a Google service account."
    }
  }
}
