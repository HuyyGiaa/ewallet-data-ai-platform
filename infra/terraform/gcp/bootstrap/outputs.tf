output "state_bucket_name" {
  description = "Bucket passed to the development stack's GCS backend."
  value       = google_storage_bucket.terraform_state.name
}

output "state_bucket_url" {
  description = "GCS URL of the Terraform state bucket."
  value       = google_storage_bucket.terraform_state.url
}
