variable "project_id" {
  description = "GCP project that owns platform storage."
  type        = string
}

variable "region" {
  description = "Single region for data locality and controlled cost."
  type        = string
}

variable "data_bucket_name" {
  description = "Canonical source, lakehouse, feature and ML artifact bucket."
  type        = string
}

variable "temporary_bucket_name" {
  description = "Regenerable staging and temporary artifact bucket."
  type        = string
}

variable "temporary_retention_days" {
  description = "Age after which regenerable temporary objects are deleted."
  type        = number
  default     = 7

  validation {
    condition     = var.temporary_retention_days >= 1 && var.temporary_retention_days <= 30
    error_message = "temporary_retention_days must be between 1 and 30."
  }
}
