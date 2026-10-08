path "secret/data/port/api/pii-envelope" {
  capabilities = ["read"]
}

path "transit/decrypt/port-pii-kek" {
  capabilities = ["update"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}

# Application data stays encrypted at rest. Key administration remains operator-only.
path "transit/encrypt/port-private-data" {
  capabilities = ["update"]
}

path "transit/decrypt/port-private-data" {
  capabilities = ["update"]
}

path "transit/hmac/port-private-lookup/sha2-256" {
  capabilities = ["update"]
}
