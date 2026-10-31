variable "project_id" {
  description = "GCP project in which APIs are enabled."
  type        = string
}

variable "services" {
  description = "Service APIs required by the cloud platform."
  type        = set(string)
}
