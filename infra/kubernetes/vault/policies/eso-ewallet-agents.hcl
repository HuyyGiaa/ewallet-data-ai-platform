# External Secrets Operator reader for namespace ewallet-agents (staged, opt-in).
# One shadow copy of the gateway client key; the live agentgateway-client-key Secret and
# the MCP credential releases are not managed through Vault.
path "ewallet/data/agent-platform/agentgateway-client-key" {
  capabilities = ["read"]
}

# token_no_default_policy removes these; ESO checks its login token and revokes it after use.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
