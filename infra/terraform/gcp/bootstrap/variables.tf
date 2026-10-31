variable "project_id" {
  description = "GCP project that owns the Terraform state bucket."
  type        = string
  default     = "ewallet-data-ai-platform-hg26"

  validation {
    condition     = var.project_id == "ewallet-data-ai-platform-hg26"
    error_message = "R2 is locked to project ewallet-data-ai-platform-hg26."
  }
}

variable "region" {
  description = "Single region used by state and workload resources."
  type        = string
  default     = "asia-southeast1"
}

variable "state_bucket_name" {
  description = "Globally unique GCS bucket used only for Terraform state."
  type        = string
  default     = "ewallet-data-ai-platform-hg26-tfstate"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be a valid globally unique GCS bucket name."
  }
}
