#!/bin/sh
# This copies the SAME pre-existing static seal key. It never creates a key,
# initializes Bao, migrates a seal, reads macOS Keychain, or replaces raft data.
set +x
set -eu
umask 077

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

source_file="${OPENBAO_STATIC_SEAL_FILE:-}"
encoded="${OPENBAO_STATIC_SEAL_KEY_B64:-}"
unset OPENBAO_STATIC_SEAL_KEY_B64
if [ -n "$source_file" ] && [ -n "$encoded" ]; then
  fail 'provide exactly one existing static seal key source'
fi
if [ -z "$source_file" ] && [ -z "$encoded" ]; then
  fail 'existing static seal key unavailable; generation and reinitialization are forbidden'
fi
if [ "${OPENBAO_SEAL_MODE:-static}" != 'static' ]; then
  fail 'portable seal entrypoint requires static mode with the existing seal key ID'
fi
export OPENBAO_SEAL_MODE=static

runtime="${OPENBAO_SEAL_RUNTIME_DIR:-/bao/seal-runtime}"
case "$runtime" in
  /bao/seal-runtime)
    [ -d "$runtime" ] && [ ! -L "$runtime" ] || fail 'static seal runtime must be an existing tmpfs directory'
    ;;
  /dev/shm/port-bao-seal)
    # Railway has no tmpfs manifest option; use actual Linux shared memory.
    [ -d /dev/shm ] && [ ! -L /dev/shm ] || fail 'platform shared-memory filesystem unavailable'
    [ "$(stat -f -c %T /dev/shm)" = 'tmpfs' ] || fail 'platform shared-memory filesystem must be tmpfs'
    [ ! -L "$runtime" ] || fail 'platform seal runtime path is unsafe'
    mkdir -p "$runtime"
    ;;
  *) fail 'unsupported static seal runtime directory' ;;
esac
[ "$(stat -f -c %T "$runtime")" = 'tmpfs' ] || fail 'static seal runtime must be tmpfs, never a persistent key volume'
chown openbao:openbao "$runtime"
chmod 700 "$runtime"
temporary=$(mktemp "$runtime/.incoming.XXXXXXXX")
trap 'rm -f "$temporary"' EXIT HUP INT TERM

if [ -n "$source_file" ]; then
  case "$source_file" in
    /*) ;;
    *) fail 'existing static seal key source must be an absolute file path' ;;
  esac
  [ -f "$source_file" ] && [ ! -L "$source_file" ] || fail 'existing static seal key source must be a regular non-symlink file'
  # The original provider/key-export operation is an explicit external approval.
  # This server only consumes its supplied key; there is no Keychain fallback.
  cat "$source_file" > "$temporary"
else
  printf '%s' "$encoded" | base64 -d > "$temporary" 2>/dev/null || fail 'invalid platform static seal secret encoding'
  [ "$(base64 < "$temporary" | tr -d '\n')" = "$encoded" ] || fail 'platform static seal secret must use canonical base64'
fi
encoded=''
unset OPENBAO_STATIC_SEAL_FILE
[ "$(wc -c < "$temporary" | tr -d '[:space:]')" = '32' ] || fail 'existing static seal key must be exactly 32 bytes'
chown openbao:openbao "$temporary"
chmod 400 "$temporary"

key="$runtime/current.key"
if [ -e "$key" ] || [ -L "$key" ]; then
  [ -f "$key" ] && [ ! -L "$key" ] || fail 'existing runtime seal key path is unsafe'
  cmp -s "$temporary" "$key" || fail 'supplied seal key does not match existing runtime key; replacement forbidden'
  chown openbao:openbao "$key"
  chmod 400 "$key"
else
  # link is an atomic no-replace publication; another publisher cannot be erased.
  if ! ln "$temporary" "$key" 2>/dev/null; then
    [ -f "$key" ] && [ ! -L "$key" ] && cmp -s "$temporary" "$key" || fail 'concurrent seal key publication does not match'
  fi
fi
rm -f "$temporary"
trap - EXIT HUP INT TERM
[ "$#" -gt 0 ] || fail 'OpenBao server command is required'
exec "$@"
