variable "enabled" {
  description = "Explicit cost gate for the Jenkins VM."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect the VM until a separately reviewed cleanup plan disables it."
  type        = bool
  default     = true
}

variable "project_id" {
  description = "GCP project that owns the VM."
  type        = string
}

variable "zone" {
  description = "Compute zone."
  type        = string
}

variable "instance_name" {
  description = "Jenkins VM name."
  type        = string
}

variable "machine_type" {
  description = "CPU-only Jenkins machine type."
  type        = string
  default     = "e2-standard-2"
}

variable "boot_disk_size_gb" {
  description = "Balanced boot disk size."
  type        = number
  default     = 50
}

variable "network_self_link" {
  description = "VPC self link."
  type        = string
}

variable "subnet_self_link" {
  description = "Subnet self link."
  type        = string
}

variable "service_account_email" {
  description = "Dedicated CI identity attached to the VM."
  type        = string
}

variable "ssh_source_ranges" {
  description = "CIDRs allowed to reach SSH; default is Google IAP TCP forwarding."
  type        = list(string)
  default     = ["35.235.240.0/20"]
}
