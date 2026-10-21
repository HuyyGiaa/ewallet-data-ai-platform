output "service_account_emails" {
  description = "Workload service-account emails keyed by responsibility."
  value = {
    for key, account in google_service_account.workload : key => account.email
  }
}

output "spark_service_account_email" {
  description = "Identity used by Serverless for Apache Spark batches."
  value       = google_service_account.workload["spark"].email
}

output "gke_node_service_account_email" {
  description = "Identity attached to GKE nodes."
  value       = google_service_account.workload["gke_nodes"].email
}

output "ci_service_account_email" {
  description = "Identity attached to the optional Jenkins VM."
  value       = google_service_account.workload["ci"].email
}
