path "transit/encrypt/port-rag-private-data" {
  capabilities = ["update"]
}
path "transit/decrypt/port-rag-private-data" {
  capabilities = ["update"]
}
path "transit/hmac/port-rag-private-lookup/sha2-256" {
  capabilities = ["update"]
}
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
