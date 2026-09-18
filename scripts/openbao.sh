#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)
SNAPSHOT_DIR=${OPENBAO_SNAPSHOT_DIR:-"${ROOT_DIR}/data/openbao/snapshots"}

if [[ -n "${COMPOSE:-}" ]]; then
  read -r -a compose_command <<<"${COMPOSE}"
elif docker compose version >/dev/null 2>&1; then
  compose_command=(docker compose)
else
  compose_command=(docker-compose)
fi

compose() {
  "${compose_command[@]}" "$@"
}

usage() {
  cat >&2 <<'EOF'
Usage: scripts/openbao.sh <up|status|init|unseal|snapshot|down|logs>

init requires OPENBAO_INIT_OUTPUT to be an absolute path outside this
repository and writes the one-time initialization response there.
unseal prompts for one share without putting it in the command arguments.
snapshot prompts for a token without saving it in the repository.
EOF
}

repository_boundaries=()

load_repository_boundaries() {
  local git_common_dir primary_root worktree_path resolved_worktree
  repository_boundaries=("${ROOT_DIR}")

  git_common_dir=$(git -C "${ROOT_DIR}" rev-parse --git-common-dir 2>/dev/null || true)
  if [[ -n "${git_common_dir}" ]]; then
    if [[ "${git_common_dir}" != /* ]]; then
      git_common_dir="${ROOT_DIR}/${git_common_dir}"
    fi
    if git_common_dir=$(cd -- "${git_common_dir}" 2>/dev/null && pwd -P); then
      if primary_root=$(cd -- "${git_common_dir}/.." 2>/dev/null && pwd -P); then
        repository_boundaries+=("${primary_root}")
      fi
    fi
  fi

  while IFS= read -r worktree_path; do
    [[ -n "${worktree_path}" ]] || continue
    if resolved_worktree=$(cd -- "${worktree_path}" 2>/dev/null && pwd -P); then
      repository_boundaries+=("${resolved_worktree}")
    fi
  done < <(git -C "${ROOT_DIR}" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p')
}

path_is_repository_path() {
  local boundary
  for boundary in "${repository_boundaries[@]}"; do
    if [[ "${1}" == "${boundary}" || "${1}" == "${boundary}"/* ]]; then
      return 0
    fi
  done
  return 1
}

initialize_cluster() {
  if [[ -z "${OPENBAO_INIT_OUTPUT:-}" ]]; then
    echo "OPENBAO_INIT_OUTPUT must name a new owner-only file outside the repository" >&2
    return 2
  fi
  if [[ "${OPENBAO_INIT_OUTPUT}" != /* ]]; then
    echo "OPENBAO_INIT_OUTPUT must be an absolute path" >&2
    return 2
  fi

  local output_name output_parent output_path
  output_name=$(basename -- "${OPENBAO_INIT_OUTPUT}")
  output_parent=$(dirname -- "${OPENBAO_INIT_OUTPUT}")
  if [[ ! -d "${output_parent}" ]]; then
    echo "OPENBAO_INIT_OUTPUT parent directory does not exist: ${output_parent}" >&2
    return 2
  fi
  output_parent=$(cd -- "${output_parent}" && pwd -P)
  output_path="${output_parent}/${output_name}"
  load_repository_boundaries
  if path_is_repository_path "${output_parent}"; then
    echo "OPENBAO_INIT_OUTPUT must be outside the repository and server mounts" >&2
    return 2
  fi
  if [[ -e "${output_path}" || -L "${output_path}" ]]; then
    echo "OPENBAO_INIT_OUTPUT already exists; refusing to replace it: ${output_path}" >&2
    return 2
  fi

  temporary_output=$(mktemp "${output_path}.tmp.XXXXXXXX")
  cleanup_initialization_output() {
    local exit_code=$?
    if [[ -n "${temporary_output:-}" && -e "${temporary_output}" ]]; then
      chmod 600 "${temporary_output}" 2>/dev/null || true
      if [[ "${exit_code}" -eq 0 ]]; then
        rm -f -- "${temporary_output}"
      else
        echo "OpenBao initialization response preserved for recovery at ${temporary_output}" >&2
      fi
    fi
    return "${exit_code}"
  }
  trap cleanup_initialization_output EXIT
  chmod 600 "${temporary_output}"
  initialization_status=0
  if compose exec -T openbao bao operator init -key-shares=5 -key-threshold=3 >"${temporary_output}" 2>&1; then
    initialization_status=0
  else
    initialization_status=$?
  fi
  chmod 600 "${temporary_output}"
  if [[ "${initialization_status}" -ne 0 ]]; then
    echo "OpenBao initialization failed with exit code ${initialization_status}; response was preserved for recovery" >&2
    return "${initialization_status}"
  fi
  if ! ln -- "${temporary_output}" "${output_path}"; then
    echo "OPENBAO_INIT_OUTPUT was created concurrently; refusing to replace it: ${output_path}" >&2
    return 2
  fi
  rm -f -- "${temporary_output}"
  temporary_output=""
  trap - EXIT
  echo "OpenBao initialization output saved to ${output_path}"
}

command_name=${1:-}
cd "${ROOT_DIR}"
mkdir -p "${SNAPSHOT_DIR}"
SNAPSHOT_DIR=$(cd -- "${SNAPSHOT_DIR}" && pwd -P)
chmod 700 "${SNAPSHOT_DIR}"
export OPENBAO_SNAPSHOT_DIR="${SNAPSHOT_DIR}"

case "${command_name}" in
  up)
    bash "${SCRIPT_DIR}/openbao-tls.sh"
    compose up -d openbao
    ;;
  status)
    set +e
    compose exec -T openbao bao status -format=json
    status_code=$?
    set -e
    if [[ "${status_code}" -ne 0 && "${status_code}" -ne 2 ]]; then
      exit "${status_code}"
    fi
    ;;
  init)
    initialize_cluster
    ;;
  unseal)
    compose exec openbao bao operator unseal
    ;;
  snapshot)
    read -r -s -p "OpenBao token: " BAO_TOKEN
    printf '\n' >&2
    export BAO_TOKEN
    trap 'unset BAO_TOKEN' EXIT
    snapshot_name="openbao-$(date -u +%Y%m%dT%H%M%SZ).snap"
    if [[ -e "${SNAPSHOT_DIR}/${snapshot_name}" ]]; then
      echo "OpenBao snapshot already exists: ${SNAPSHOT_DIR}/${snapshot_name}" >&2
      exit 1
    fi
    compose exec -T --user 0:0 -e BAO_TOKEN openbao bao operator raft snapshot save "/bao/snapshots/${snapshot_name}"
    echo "OpenBao snapshot saved to ${SNAPSHOT_DIR}/${snapshot_name}"
    ;;
  down)
    compose stop openbao
    ;;
  logs)
    compose logs --tail=100 openbao
    ;;
  *)
    usage
    exit 2
    ;;
esac
