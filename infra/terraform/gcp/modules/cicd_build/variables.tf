variable "project_id" {
  description = "GCP project that hosts the builds, bucket and service accounts."
  type        = string
}

variable "region" {
  description = "Region of the build bucket and the Artifact Registry repository."
  type        = string
}

variable "artifact_repository_id" {
  description = "The only Artifact Registry repository the build runner may write."
  type        = string
}

variable "jenkins_namespace" {
  description = "Namespace of the in-cluster Jenkins controller."
  type        = string
  default     = "ewallet-ci"
}

variable "jenkins_service_account" {
  description = "Kubernetes ServiceAccount of the Jenkins controller."
  type        = string
  default     = "jenkins"
}

variable "build_object_retention_days" {
  description = "Days before staged sources and build logs are deleted."
  type        = number
  default     = 30
}
