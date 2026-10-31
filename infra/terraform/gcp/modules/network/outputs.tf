output "network_name" {
  description = "Custom VPC name."
  value       = google_compute_network.platform.name
}

output "network_self_link" {
  description = "Custom VPC self link."
  value       = google_compute_network.platform.self_link
}

output "subnet_name" {
  description = "Regional subnet name."
  value       = google_compute_subnetwork.platform.name
}

output "subnet_self_link" {
  description = "Regional subnet self link."
  value       = google_compute_subnetwork.platform.self_link
}
