#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${OFFICE_API_MOCK_SOURCE_DIR:-}" ]]; then
  infra_root="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
  OFFICE_API_MOCK_SOURCE_DIR="$(dirname "$(dirname "${infra_root}")")/office/api-mock"
  export OFFICE_API_MOCK_SOURCE_DIR
elif [[ "${OFFICE_API_MOCK_SOURCE_DIR}" != /* ]]; then
  echo "OFFICE_API_MOCK_SOURCE_DIR must be an absolute path" >&2
  exit 1
fi

if [[ -n "${COMPOSE:-}" ]]; then
  read -r -a compose_command <<<"${COMPOSE}"
elif docker compose version >/dev/null 2>&1; then
  compose_command=(docker compose)
else
  compose_command=(docker-compose)
fi

config="$("${compose_command[@]}" -f compose.office-api-mock.yml -p office-api-mock config --format json)"

jq -e '
  .name == "office-api-mock"
  and .services."office-api-mock".image == "office-api-mock:local"
  and .services."office-api-mock".build.context == env.OFFICE_API_MOCK_SOURCE_DIR
  and .services."office-api-mock".environment.DB_PATH == "/data/app.db"
  and .services."office-api-mock".environment.SEED_MODE == "if-empty"
  and (.services."office-api-mock".ports | any((.host_ip == "127.0.0.1") and ((.published | tonumber) == 18080) and (.target == 8080)))
  and (.services."office-api-mock".volumes | any(.source == "office_api_mock_data" and .target == "/data"))
  and .services."office-api-mock".healthcheck.test == ["CMD", "wget", "-qO-", "http://127.0.0.1:8080/health"]
  and (.services."office-api-mock".networks | has("infra_default"))
  and (.services."office-api-mock".networks.infra_default.aliases | index("office-api-mock.test") != null)
  and .networks.infra_default.external == true
  and .networks.infra_default.name == "infra_default"
  and (.services."office-api-mock".restart == "unless-stopped")
' >/dev/null <<<"${config}"

printf 'office-api-mock compose contract: ok\n'
