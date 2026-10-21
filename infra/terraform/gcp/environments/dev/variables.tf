variable "project_id" {
  description = "GCP project for the E-Wallet coursework platform."
  type        = string
  default     = "ewallet-data-ai-platform-hg26"

  validation {
    condition     = var.project_id == "ewallet-data-ai-platform-hg26"
    error_message = "R2 is locked to project ewallet-data-ai-platform-hg26."
  }
}

variable "region" {
  description = "Primary GCP region."
  type        = string
  default     = "asia-southeast1"
}

variable "zone" {
  description = "Primary development zone."
  type        = string
  default     = "asia-southeast1-b"
}

variable "data_bucket_name" {
  description = "Optional globally unique override for canonical storage."
  type        = string
  default     = null
  nullable    = true
}

variable "temporary_bucket_name" {
  description = "Optional globally unique override for temporary storage."
  type        = string
  default     = null
  nullable    = true
}

variable "subnet_cidr" {
  description = "Primary CIDR for the development platform subnet."
  type        = string
  default     = "10.20.0.0/20"
}

variable "dataproc_submitter_members" {
  description = "Optional user/group/service-account principals allowed to act as the Spark identity."
  type        = set(string)
  default     = []
}

variable "enable_gke" {
  description = "Explicitly allow creation of billable GKE resources."
  type        = bool
  default     = false
}

variable "enable_jenkins_vm" {
  description = "Explicitly allow creation of the billable Jenkins VM."
  type        = bool
  default     = false
}

variable "gke_deletion_protection" {
  description = "Keep the GKE cluster protected until a separate cleanup plan turns this off."
  type        = bool
  default     = true
}

variable "jenkins_deletion_protection" {
  description = "Keep the Jenkins VM protected until a separate cleanup plan turns this off."
  type        = bool
  default     = true
}

variable "gke_machine_type" {
  description = "CPU-only machine type used only when GKE is enabled."
  type        = string
  default     = "e2-standard-4"
}

variable "gke_master_authorized_cidrs" {
  description = "Administrator /32 CIDRs allowed to reach the GKE control plane when enabled."
  type        = list(string)
  default     = []
}

variable "gke_min_nodes" {
  description = "Minimum CPU node count when GKE is enabled."
  type        = number
  default     = 0
}

variable "gke_max_nodes" {
  description = "Maximum CPU node count when GKE is enabled."
  type        = number
  default     = 3

  validation {
    condition     = var.gke_max_nodes >= 1 && var.gke_max_nodes <= 5
    error_message = "gke_max_nodes must be between 1 and 5 for the coursework cost guardrail."
  }
}

variable "jenkins_machine_type" {
  description = "CPU-only machine type used only when Jenkins is enabled."
  type        = string
  default     = "e2-standard-2"
}
