output "batch_runtime" {
  description = "Non-secret defaults passed to Dataproc batch submission."
  value       = terraform_data.batch_prerequisites.output
}
