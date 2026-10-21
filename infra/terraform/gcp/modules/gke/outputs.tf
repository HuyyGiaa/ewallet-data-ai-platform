output "cluster_name" {
  description = "GKE cluster name, or null while the cost gate is disabled."
  value       = try(google_container_cluster.platform[0].name, null)
}

output "cluster_location" {
  description = "GKE cluster zone, or null while disabled."
  value       = try(google_container_cluster.platform[0].location, null)
}

output "workload_pool" {
  description = "Workload Identity pool used by later Helm releases."
  value       = var.enabled ? "${var.project_id}.svc.id.goog" : null
}
