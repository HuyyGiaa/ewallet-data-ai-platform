# GKE technical rehearsal only (docs/runbooks/edai2/VAULT_GKE_REHEARSAL.md): the ESO reader of the
# temporary namespace vault-rehearsal reads one synthetic entry. The path lies under feature-api/,
# which the existing secret-operator token may write, so the rehearsal seeds it exactly like the
# official campaign (put --token-file). The real feature-api/feast-redis is NOT readable here.
path "ewallet/data/feature-api/rehearsal-redis" {
  capabilities = ["read"]
}

# token_no_default_policy removes these; ESO checks its login token and revokes it after use.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
