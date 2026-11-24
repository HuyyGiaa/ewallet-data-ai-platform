# Vault 2.x authenticates root generation. This policy lets the owner's admin login
# (role vault-admin) start, read, cancel and feed a root-generation ceremony, and nothing
# else: no secrets, no policies, no roles. Completing the ceremony still needs 2 of the
# 3 unseal shares. Never attach it to ESO readers or application roles.
path "sys/generate-root/attempt" {
  capabilities = ["create", "read", "update", "delete"]
}
path "sys/generate-root/update" {
  capabilities = ["create", "update"]
}
