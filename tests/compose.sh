#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${COMPOSE:-}" ]]; then
  read -r -a compose_command <<<"${COMPOSE}"
elif docker compose version >/dev/null 2>&1; then
  compose_command=(docker compose)
else
  compose_command=(docker-compose)
fi

config="$("${compose_command[@]}" --env-file .env.example -f compose.yml --profile observability --profile tools config --format json)"

jq -e '
  .services.postgres
  and .services.redis
  and .services.keycloak
  and ([.services[] | has("build")] | any | not)
' >/dev/null <<<"${config}"

jq -e '
  .services.redis.ports[0].published == "6379"
  and .services.pgadmin.profiles == ["tools"]
' >/dev/null <<<"${config}"

jq -e '
  .services.keycloak.depends_on.postgres.condition == "service_healthy"
  and .services.keycloak.environment.KC_DB == "postgres"
  and .services.keycloak.environment.KC_DB_URL_HOST == "postgres"
  and .services.keycloak.environment.KC_DB_URL_DATABASE == "keycloak"
  and .services.keycloak.ports[0].published == "18080"
  and (.services.keycloak.volumes | any(.target == "/opt/keycloak/data/import"))
  and (.services.keycloak.volumes | any(.target == "/opt/keycloak/themes/overthinker"))
' >/dev/null <<<"${config}"

jq -e '
  .services.prometheus.profiles == ["observability"]
  and .services.grafana.profiles == ["observability"]
  and (.services.grafana.volumes | any(.target == "/etc/grafana/provisioning"))
' >/dev/null <<<"${config}"

jq -e '
  .realm == "overthinker"
  and .loginTheme == "overthinker"
  and (.clients | any(.clientId == "overthinker-api"))
  and (.clients | any(
    .clientId == "overthinker-api-bff"
    and .secret == "overthinker-local-bff-secret"
    and (.protocolMappers | any(.config."included.client.audience" == "overthinker-api"))
  ))
  and (.roles.client."overthinker-api" | map(.name) | sort == ["admin", "user"])
  and (.users | any(.username == "admin@overthinker.local"))
' keycloak/import/overthinker-realm.json >/dev/null

printf 'compose contract: ok\n'
