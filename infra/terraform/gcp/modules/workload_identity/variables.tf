variable "enabled" {
  description = "Create GKE Workload Identity bindings only after the cluster pool exists."
  type        = bool
  default     = false
}

variable "project_id" {
  description = "GCP project that owns the GKE workload pool and service accounts."
  type        = string
}

variable "service_account_emails" {
  description = "Google service-account emails keyed by workload responsibility."
  type        = map(string)
}
