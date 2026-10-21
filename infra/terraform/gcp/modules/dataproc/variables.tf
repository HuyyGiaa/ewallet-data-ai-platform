variable "project_id" {
  description = "GCP project used by Serverless for Apache Spark batches."
  type        = string
}

variable "region" {
  description = "Region in which batches run."
  type        = string
}

variable "subnet_self_link" {
  description = "Subnet with Private Google Access used by batches."
  type        = string
}

variable "service_account_email" {
  description = "Least-privilege Spark runtime identity."
  type        = string
}

variable "temporary_bucket_name" {
  description = "Bucket for regenerable Spark staging and temporary data."
  type        = string
}
