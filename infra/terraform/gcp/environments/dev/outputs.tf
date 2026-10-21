output "project_id" {
  description = "GCP project used by runtime configuration."
  value       = var.project_id
}

output "region" {
  description = "Primary GCP region."
  value       = var.region
}

output "data_bucket_name" {
  description = "EWALLET_DATA_BUCKET value for dev/gcp runtime profiles."
  value       = module.storage.data_bucket_name
}

output "temporary_bucket_name" {
  description = "Regenerable staging bucket."
  value       = module.storage.temporary_bucket_name
}

output "artifact_registry_url" {
  description = "Docker image prefix used by CI."
  value       = module.artifact_registry.repository_url
}

output "network_name" {
  description = "VPC name used by cloud batch and cluster workloads."
  value       = module.network.network_name
}

output "subnet_name" {
  description = "Regional subnet name used by cloud workloads."
  value       = module.network.subnet_name
}

output "service_account_emails" {
  description = "Workload identities keyed by responsibility."
  value       = module.iam.service_account_emails
}

output "dataproc_batch_runtime" {
  description = "Non-secret defaults for Serverless for Apache Spark submissions."
  value       = module.dataproc.batch_runtime
}

output "gke_cluster_name" {
  description = "Cluster name, or null while GKE is cost-gated off."
  value       = module.gke.cluster_name
}

output "jenkins_instance_name" {
  description = "Jenkins VM name, or null while the VM is cost-gated off."
  value       = module.jenkins_vm.instance_name
}
