#!/bin/sh
set +x
set -eu
umask 077

# Existing production data and certificates must be explicitly mounted by the
# operator. Never initialize missing raft storage or generate replacement TLS.
for directory in /bao/data /bao/audit /bao/tls; do
  [ -d "$directory" ] && [ ! -L "$directory" ] || {
    echo 'original Bao data/audit/TLS mount unavailable' >&2
    exit 1
  }
done
for name in ca.crt server.crt server.key; do
  [ -f "/bao/tls/$name" ] && [ ! -L "/bao/tls/$name" ] || {
    echo 'original Bao TLS asset unavailable' >&2
    exit 1
  }
done
[ -n "$(find /bao/data -mindepth 1 -type f -print -quit)" ] || {
  echo 'existing initialized raft data required; reinitialization is forbidden' >&2
  exit 1
}
[ -f /dev/shm/port-bao-seal/current.key ] && [ ! -L /dev/shm/port-bao-seal/current.key ] || {
  echo 'same existing static seal key unavailable' >&2
  exit 1
}
mkdir -p /bao/tls-runtime
[ ! -L /bao/tls-runtime ] || exit 1
for name in ca.crt server.crt server.key; do
  cp "/bao/tls/$name" "/bao/tls-runtime/$name"
done
chown -R openbao:openbao /bao/tls-runtime
chmod 700 /bao/tls-runtime
chmod 644 /bao/tls-runtime/ca.crt /bao/tls-runtime/server.crt
chmod 400 /bao/tls-runtime/server.key
exec su-exec openbao:openbao bao server -config=/bao/config/openbao.hcl -config=/bao/config/seal-platform.hcl
