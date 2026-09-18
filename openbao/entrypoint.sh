#!/bin/sh
set -eu

seal_mode="${OPENBAO_SEAL_MODE:-shamir}"
case "${seal_mode}" in
  shamir|static)
    ;;
  *)
    echo "invalid OPENBAO_SEAL_MODE: ${seal_mode}" >&2
    exit 1
    ;;
esac

chown -R openbao:openbao /bao/data /bao/audit
mkdir -p /bao/tls-runtime
cp /bao/tls/ca.crt /bao/tls-runtime/ca.crt
cp /bao/tls/server.crt /bao/tls-runtime/server.crt
cp /bao/tls/server.key /bao/tls-runtime/server.key
chown -R openbao:openbao /bao/tls-runtime
chmod 700 /bao/tls-runtime
chmod 644 /bao/tls-runtime/ca.crt /bao/tls-runtime/server.crt
chmod 600 /bao/tls-runtime/server.key

if [ "${seal_mode}" = "static" ]; then
  seal_runtime_dir=/bao/seal-runtime
  seal_key="${seal_runtime_dir}/current.key"
  static_config=/bao/config/seal-static.hcl

  mkdir -p "${seal_runtime_dir}"
  chown openbao:openbao "${seal_runtime_dir}"
  chmod 700 "${seal_runtime_dir}"
  test -r "${static_config}"

  # The host controller publishes current.key atomically after reading the
  # keychain. A missing key means the controller has not run yet; an existing
  # invalid path or length is a hard failure.
  while [ ! -e "${seal_key}" ]; do
    sleep 1
  done
  if [ -L "${seal_key}" ] || [ ! -f "${seal_key}" ]; then
    echo "static seal key must be a regular file" >&2
    exit 1
  fi
  key_length="$(wc -c < "${seal_key}" | tr -d '[:space:]')"
  if [ "${key_length}" != "32" ]; then
    echo "static seal key must be exactly 32 bytes" >&2
    exit 1
  fi
  chown openbao:openbao "${seal_key}"
  chmod 0400 "${seal_key}"
  if [ "$(stat -c '%U' "${seal_key}")" != "openbao" ] || [ "$(stat -c '%a' "${seal_key}")" != "400" ]; then
    echo "static seal key ownership or permissions are invalid" >&2
    exit 1
  fi
  set -- "$@" "-config=${static_config}"
fi

exec su-exec openbao:openbao bao "$@"
