# The only External Secrets Operator reader (ClusterSecretStore ewallet-vault, role eso-ewallet-reader).
# Read only, on two explicit prefixes. No list and no metadata, so the reader cannot discover
# paths; no system paths, no write. A new area (for example mcp-platform/) is a reviewed change here.
path "ewallet/data/ml-platform/*" {
  capabilities = ["read"]
}
path "ewallet/data/agent-platform/*" {
  capabilities = ["read"]
}

# token_no_default_policy removes these; ESO checks its login token and revokes it after use.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
