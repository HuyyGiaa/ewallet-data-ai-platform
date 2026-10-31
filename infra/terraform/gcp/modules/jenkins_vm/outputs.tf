output "instance_name" {
  description = "Jenkins VM name, or null while disabled."
  value       = try(google_compute_instance.jenkins[0].name, null)
}

output "external_ip" {
  description = "Ephemeral egress IP, or null while disabled."
  value       = try(google_compute_instance.jenkins[0].network_interface[0].access_config[0].nat_ip, null)
}
