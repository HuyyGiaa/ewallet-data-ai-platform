variable "project_id" {
  description = "GCP project that owns the repository."
  type        = string
}

variable "region" {
  description = "Artifact Registry region."
  type        = string
}

variable "repository_id" {
  description = "Docker repository identifier."
  type        = string
}
