#!/bin/sh
set -eu

chown -R openbao:openbao /bao/data /bao/audit
mkdir -p /bao/tls-runtime
cp /bao/tls/ca.crt /bao/tls-runtime/ca.crt
cp /bao/tls/server.crt /bao/tls-runtime/server.crt
cp /bao/tls/server.key /bao/tls-runtime/server.key
chown -R openbao:openbao /bao/tls-runtime
chmod 700 /bao/tls-runtime
chmod 644 /bao/tls-runtime/ca.crt /bao/tls-runtime/server.crt
chmod 600 /bao/tls-runtime/server.key
exec su-exec openbao:openbao bao "$@"
