# External Secrets Operator reader for namespace ml-platform (SecretStore ml-platform/vault).
# Exactly the secrets that namespace's ExternalSecrets map, read only. No list, no metadata,
# so the reader cannot discover other paths. Add a path here only with its ExternalSecret.
path "ewallet/data/feature-api/feast-redis" {
  capabilities = ["read"]
}

# token_no_default_policy removes these; ESO checks its login token and revokes it after use.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
