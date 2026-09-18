path "secret/data/port/api/pii-envelope" {
  capabilities = ["read"]
}

path "transit/decrypt/port-pii-kek" {
  capabilities = ["update"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
