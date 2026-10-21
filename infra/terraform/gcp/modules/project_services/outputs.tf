output "enabled_services" {
  description = "Service APIs managed by this stack."
  value       = sort([for service in google_project_service.required : service.service])
}
