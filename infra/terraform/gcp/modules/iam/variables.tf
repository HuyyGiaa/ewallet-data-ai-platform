variable "project_id" {
  description = "GCP project that owns workload identities."
  type        = string
}

variable "data_bucket_name" {
  description = "Canonical platform bucket for bucket-scoped IAM."
  type        = string
}

variable "temporary_bucket_name" {
  description = "Regenerable staging bucket for bucket-scoped IAM."
  type        = string
}

variable "dataproc_submitter_members" {
  description = "Additional IAM principals allowed to submit batches as the Spark service account."
  type        = set(string)
  default     = []

  validation {
    condition = alltrue([
      for member in var.dataproc_submitter_members :
      can(regex("^(user|serviceAccount|group):[^[:space:]]+$", member))
    ])
    error_message = "Submitters must use an explicit user:, group: or serviceAccount: IAM member prefix."
  }
}
