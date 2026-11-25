# Rotation by a human operator: write new versions and read metadata, never values.
# The two V2 areas only (docs/edai2/platform/VAULT_V2_ARCHITECTURE_AUDIT.md §10).
path "ewallet/data/ml-platform/*" {
  capabilities = ["create", "update"]
}
path "ewallet/metadata/ml-platform/*" {
  capabilities = ["read", "list"]
}
path "ewallet/data/agent-platform/*" {
  capabilities = ["create", "update"]
}
path "ewallet/metadata/agent-platform/*" {
  capabilities = ["read", "list"]
}
