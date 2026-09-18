ui = false

api_addr     = "https://openbao:8200"
cluster_addr = "https://openbao:8201"

listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"

  tls_disable     = false
  tls_cert_file   = "/bao/tls-runtime/server.crt"
  tls_key_file    = "/bao/tls-runtime/server.key"
}

storage "raft" {
  path    = "/bao/data"
  node_id = "openbao-1"
}

audit "file" "json" {
  description = "OpenBao JSON audit log"
  options = {
    file_path = "/bao/audit/audit.log"
    mode      = "0640"
  }
}
