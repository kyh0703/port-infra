#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)
TLS_DIR=${OPENBAO_TLS_DIR:-"${ROOT_DIR}/data/openbao/tls"}

required_files=(ca.crt server.crt server.key)
mkdir -p "${TLS_DIR}"
chmod 700 "${TLS_DIR}"

present_files=()
for file in "${required_files[@]}"; do
  if [[ -e "${TLS_DIR}/${file}" ]]; then
    present_files+=("${file}")
  fi
done

if ((${#present_files[@]} > 0 && ${#present_files[@]} < ${#required_files[@]})); then
  echo "OpenBao TLS directory is incomplete; refusing to replace existing files: ${TLS_DIR}" >&2
  exit 1
fi

if ((${#present_files[@]} == ${#required_files[@]})); then
  if ! openssl verify -CAfile "${TLS_DIR}/ca.crt" "${TLS_DIR}/server.crt" >/dev/null 2>&1; then
    echo "OpenBao TLS material is invalid; refusing to replace existing files: ${TLS_DIR}" >&2
    exit 1
  fi

  certificate_text=$(openssl x509 -in "${TLS_DIR}/server.crt" -noout -ext subjectAltName 2>/dev/null || true)
  for name in "DNS:openbao" "DNS:localhost" "IP Address:127.0.0.1"; do
    if [[ "${certificate_text}" != *"${name}"* ]]; then
      echo "OpenBao server certificate is missing required SAN ${name}; refusing to replace existing files" >&2
      exit 1
    fi
  done

  chmod 600 "${TLS_DIR}/server.key"
  chmod 644 "${TLS_DIR}/ca.crt" "${TLS_DIR}/server.crt"
  exit 0
fi

umask 077
tmp_dir=$(mktemp -d "${TLS_DIR}/.generate.XXXXXXXX")
cleanup() {
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

openssl req -x509 -newkey rsa:4096 -nodes -sha256 -days 3650 \
  -keyout "${tmp_dir}/ca.key" \
  -out "${tmp_dir}/ca.crt" \
  -subj "/CN=port-infra-openbao-local-ca" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:1" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  >/dev/null 2>&1

openssl req -new -newkey rsa:3072 -nodes -sha256 \
  -keyout "${tmp_dir}/server.key" \
  -out "${tmp_dir}/server.csr" \
  -subj "/CN=openbao" \
  >/dev/null 2>&1

printf '%s\n' \
  'basicConstraints=critical,CA:FALSE' \
  'keyUsage=critical,digitalSignature,keyEncipherment' \
  'extendedKeyUsage=serverAuth' \
  'subjectAltName=DNS:openbao,DNS:localhost,IP:127.0.0.1' \
  >"${tmp_dir}/server.ext"

openssl x509 -req -sha256 -days 825 \
  -in "${tmp_dir}/server.csr" \
  -CA "${tmp_dir}/ca.crt" \
  -CAkey "${tmp_dir}/ca.key" \
  -CAcreateserial \
  -out "${tmp_dir}/server.crt" \
  -extfile "${tmp_dir}/server.ext" \
  >/dev/null 2>&1

openssl verify -CAfile "${tmp_dir}/ca.crt" "${tmp_dir}/server.crt" >/dev/null

mv "${tmp_dir}/ca.crt" "${TLS_DIR}/ca.crt"
mv "${tmp_dir}/server.crt" "${TLS_DIR}/server.crt"
mv "${tmp_dir}/server.key" "${TLS_DIR}/server.key"
chmod 600 "${TLS_DIR}/server.key"
chmod 644 "${TLS_DIR}/ca.crt" "${TLS_DIR}/server.crt"

echo "OpenBao TLS material prepared in ${TLS_DIR}"
