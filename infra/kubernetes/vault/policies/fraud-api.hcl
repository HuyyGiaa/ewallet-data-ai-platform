# Exactly one secret, read only. No list, so a holder cannot discover other paths.
path "ewallet/data/fraud-api/feast-redis" {
  capabilities = ["read"]
}

# token_no_default_policy removes this; the loader retires its token after reading.
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
