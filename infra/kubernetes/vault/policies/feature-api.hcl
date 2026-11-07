# Exactly one secret, read only, separate from the Fraud API's path.
path "ewallet/data/feature-api/feast-redis" {
  capabilities = ["read"]
}

# token_no_default_policy removes this; the loader retires its token after reading.
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
