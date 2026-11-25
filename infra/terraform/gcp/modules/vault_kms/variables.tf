variable "enabled" {
  description = "Create the Vault Cloud KMS resources (P4-B, separate owner approval). Off by default."
  type        = bool
  default     = false
}

variable "project_id" {
  description = "GCP project that owns the key ring, key and Vault service account."
  type        = string
}
