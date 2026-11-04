output "jenkins_service_account_email" {
  description = "Workload Identity target of KSA ewallet-ci/jenkins."
  value       = google_service_account.jenkins.email
}

output "cloudbuild_service_account_email" {
  description = "Identity Cloud Build steps run as."
  value       = google_service_account.cloudbuild.email
}

output "build_bucket" {
  description = "Bucket for staged sources and build logs."
  value       = google_storage_bucket.builds.name
}

output "build_logs_bucket" {
  description = "Bucket Cloud Build writes build logs to."
  value       = google_storage_bucket.build_logs.name
}
