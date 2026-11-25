# Values for the Vault seal stanza and the KSA annotation (infra/kubernetes/vault/values-gke.yaml).
output "seal" {
  description = "gcpckms seal parameters; null while the module is disabled."
  value = var.enabled ? {
    project    = var.project_id
    region     = local.location
    key_ring   = local.key_ring_name
    crypto_key = local.crypto_key_name
  } : null
}

output "crypto_key_id" {
  description = "Full resource ID of the Vault unseal key."
  value       = one(google_kms_crypto_key.vault_unseal[*].id)
}

output "service_account_email" {
  description = "GSA for the Vault KSA annotation iam.gke.io/gcp-service-account."
  value       = one(google_service_account.vault_unseal[*].email)
}

output "workload_identity_member" {
  description = "The Vault KSA's Workload Identity principal."
  value       = var.enabled ? "serviceAccount:${var.project_id}.svc.id.goog[${local.vault_ksa}]" : null
}
