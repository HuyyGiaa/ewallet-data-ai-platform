# Rehearsal only (docs/runbooks/edai2/VAULT_GKE_REHEARSAL.md): ClusterSecretStore ewallet-vault-rehearsal,
# used from the temporary rehearsal namespaces, reads exactly three synthetic entries. They lie under the
# official prefixes, which the existing secret-operator token may write, so the rehearsal seeds them exactly
# like the official campaign (put --token-file). The real ml-platform/feast-online-redis,
# agent-platform/vllm-api-key and agent-platform/agentgateway-client-key are NOT readable here.
path "ewallet/data/ml-platform/rehearsal-redis" {
  capabilities = ["read"]
}
path "ewallet/data/agent-platform/rehearsal-vllm-api-key" {
  capabilities = ["read"]
}
path "ewallet/data/agent-platform/rehearsal-agentgateway-client-key" {
  capabilities = ["read"]
}

# token_no_default_policy removes these; ESO checks its login token and revokes it after use.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
