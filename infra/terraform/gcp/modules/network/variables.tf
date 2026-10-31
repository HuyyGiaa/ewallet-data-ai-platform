variable "project_id" {
  description = "GCP project that owns the network."
  type        = string
}

variable "region" {
  description = "Region for the platform subnet."
  type        = string
}

variable "network_name" {
  description = "Custom VPC name."
  type        = string
}

variable "subnet_name" {
  description = "Regional subnet name."
  type        = string
}

variable "subnet_cidr" {
  description = "Primary CIDR used by VMs and GKE nodes."
  type        = string

  validation {
    condition     = can(cidrhost(var.subnet_cidr, 0))
    error_message = "subnet_cidr must be a valid IPv4 CIDR."
  }
}
