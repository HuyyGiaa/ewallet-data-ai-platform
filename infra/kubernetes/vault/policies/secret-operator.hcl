# Rotation by a human operator: write new versions and read metadata, never values.
path "ewallet/data/fraud-api/*" {
  capabilities = ["create", "update"]
}
path "ewallet/metadata/fraud-api/*" {
  capabilities = ["read", "list"]
}
