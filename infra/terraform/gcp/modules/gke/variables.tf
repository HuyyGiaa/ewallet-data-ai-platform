variable "enabled" {
  description = "Explicit cost gate for GKE resources."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect the cluster until a separately reviewed cleanup plan disables it."
  type        = bool
  default     = true
}

variable "project_id" {
  description = "GCP project that owns the cluster."
  type        = string
}

variable "zone" {
  description = "Zone for the cost-bounded development cluster."
  type        = string
}

variable "cluster_name" {
  description = "GKE cluster name."
  type        = string
}

variable "network_self_link" {
  description = "VPC self link."
  type        = string
}

variable "subnet_self_link" {
  description = "Subnet self link."
  type        = string
}

variable "node_service_account_email" {
  description = "Dedicated identity attached to GKE nodes."
  type        = string
}

variable "master_authorized_cidrs" {
  description = "Explicit administrator CIDRs allowed to reach the public control-plane endpoint."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.master_authorized_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Every master_authorized_cidrs entry must be a valid CIDR."
  }
}

variable "machine_type" {
  description = "CPU-only node machine type."
  type        = string
  default     = "e2-standard-4"
}

variable "initial_node_count" {
  description = "Initial nodes when GKE is explicitly enabled."
  type        = number
  default     = 1
}

variable "min_node_count" {
  description = "Autoscaler minimum nodes per zone."
  type        = number
  default     = 0
}

variable "max_node_count" {
  description = "Autoscaler maximum nodes per zone."
  type        = number
  default     = 3
}

variable "disk_size_gb" {
  description = "Balanced persistent disk size per node."
  type        = number
  default     = 50
}
