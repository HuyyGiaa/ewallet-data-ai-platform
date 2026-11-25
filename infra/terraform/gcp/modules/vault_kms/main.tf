# Vault V2 Cloud KMS auto-unseal (docs/edai2/platform/VAULT_V2_ARCHITECTURE_AUDIT.md §8-§9, §23).
# Created only when enabled (P4-B, owner approval). No dependency on the GKE or Workload Identity
# modules: the Workload Identity principal is derived from the project ID and the fixed Vault KSA,
# so a plan targeted at this module cannot pull in the drifted node pool (ops/gcp/r2_terraform.py).
# The names must equal infra/kubernetes/vault/values-gke.yaml (seal "gcpckms" and the KSA annotation);
# tests/ops/test_vault_kms_terraform.py pins both.
locals {
  enabled            = var.enabled ? 1 : 0
  location           = "asia-southeast1"
  key_ring_name      = "ewallet-vault"
  crypto_key_name    = "vault-unseal"
  service_account_id = "ewallet-vault-unseal"
  vault_ksa          = "ewallet-vault/vault"
}

# Owned here, not in module.project_services: a targeted plan stays inside this module. Never disabled.
resource "google_project_service" "cloudkms" {
  count = local.enabled

  project                    = var.project_id
  service                    = "cloudkms.googleapis.com"
  disable_on_destroy         = false
  disable_dependent_services = false
  deletion_policy            = "ABANDON"
}

# Key rings and keys cannot be deleted in Cloud KMS; their names are taken for good.
resource "google_kms_key_ring" "vault" {
  count = local.enabled

  project  = var.project_id
  name     = local.key_ring_name
  location = local.location

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.cloudkms]
}

# Software key, no scheduled rotation (each rotation adds a billable version that must stay enabled).
# Destroying a version Vault encrypted with makes the Vault data unrecoverable, even from backups.
resource "google_kms_crypto_key" "vault_unseal" {
  count = local.enabled

  name                       = local.crypto_key_name
  key_ring                   = google_kms_key_ring.vault[0].id
  purpose                    = "ENCRYPT_DECRYPT"
  destroy_scheduled_duration = "2592000s"
  deletion_policy            = "PREVENT"

  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"
    protection_level = "SOFTWARE"
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Vault's only identity towards Google Cloud. No key is ever created for it: Vault gets short-lived
# tokens through GKE Workload Identity.
resource "google_service_account" "vault_unseal" {
  count = local.enabled

  project      = var.project_id
  account_id   = local.service_account_id
  display_name = "E-Wallet Vault auto-unseal"
  description  = "Vault gcpckms seal: encrypt/decrypt with ewallet-vault/vault-unseal only"

  deletion_policy = "PREVENT"

  lifecycle {
    prevent_destroy = true
  }
}

# Key-scoped and non-authoritative (_iam_member): no other principal on the key is touched.
# useToEncrypt + useToDecrypt.
resource "google_kms_crypto_key_iam_member" "vault_encrypter_decrypter" {
  count = local.enabled

  crypto_key_id = google_kms_crypto_key.vault_unseal[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_service_account.vault_unseal[0].email}"
}

# cloudkms.cryptoKeys.get, which the seal needs at start-up. On the key, roles/cloudkms.viewer also
# reads that key's metadata and its versions' metadata (get/list); it reads no key material and
# cannot use, change, disable or destroy anything.
resource "google_kms_crypto_key_iam_member" "vault_viewer" {
  count = local.enabled

  crypto_key_id = google_kms_crypto_key.vault_unseal[0].id
  role          = "roles/cloudkms.viewer"
  member        = "serviceAccount:${google_service_account.vault_unseal[0].email}"
}

# KSA ewallet-vault/vault (the Vault chart's server ServiceAccount) may act as the GSA. Scoped to the GSA.
resource "google_service_account_iam_member" "vault_workload_identity" {
  count = local.enabled

  service_account_id = google_service_account.vault_unseal[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${local.vault_ksa}]"
}
