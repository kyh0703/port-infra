#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${COMPOSE:-}" ]]; then
  read -r -a compose_command <<<"${COMPOSE}"
elif docker compose version >/dev/null 2>&1; then
  compose_command=(docker compose)
else
  compose_command=(docker-compose)
fi

config="$(${compose_command[@]} --env-file .env.example -f compose.yml --profile observability --profile tools config --format json)"
local_config="$(${compose_command[@]} --env-file .env.example -f compose.yml -f compose.local.yml --profile observability --profile tools config --format json)"

jq -e '
  . as $root |
  (.services.keycloak == null and .services."keycloak-db-init" == null)
  and (["postgres", "redis", "api", "web", "rag", "voice-agent", "aggregator", "adaptor", "livekit"] | all(.[]; $root.services[.] != null))
  and ([.services[] | has("build")] | any | not)
  and ([.services[]?.ports[]?.published] | any(. == "18080") | not)
' >/dev/null <<<"${config}"

jq -e '
  .services.api.depends_on.postgres.condition == "service_healthy"
  and .services.api.depends_on.redis.condition == "service_healthy"
  and (.services.api.depends_on.keycloak == null)
  and .services.api.depends_on."api-migrator".condition == "service_completed_successfully"
  and .services."api-migrator".image == "ghcr.io/kyh0703/port-api:migrator"
  and .services."api-migrator".depends_on.postgres.condition == "service_healthy"
  and .services."api-migrator".depends_on."postgres-app-init".condition == "service_completed_successfully"
  and .services."api-migrator".environment.DATABASE_URL == .services.api.environment.DATABASE_URL
' >/dev/null <<<"${config}"

jq -e '
  . as $root |
  (["DATABASE_URL", "REDIS_URL", "RAG_URL", "WEB_ORIGIN", "AUTH_PASSWORD_RESET_SECRET", "AUTH_EMAIL_VERIFICATION_SECRET", "AUTH_EMAIL_VERIFICATION_EXPOSE_DEBUG_CODE", "AUTH_RATE_LIMIT_SECRET", "USER_EMAIL_LOOKUP_KEY", "WEB_CHAT_RESUME_TOKEN_SECRET", "AUTH_SESSION_COOKIE_SECURE", "AUTH_SESSION_TTL_SECONDS", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "VOICE_RUNTIME_CREDENTIAL_ENCRYPTION_KEY", "USER_PII_ENCRYPTION_KEY"] | all(.[]; $root.services.api.environment[.] != null))
  and ([keys[] | select(test("KEYCLOAK|KC_"))] | length == 0)
  and .services.api.environment.WEB_ORIGIN == "http://macbookpro:3000"
  and .services.api.environment.LIVEKIT_URL == "ws://macbookpro:7880"
' >/dev/null <<<"${config}"

jq -e '.services.api.environment.USER_EMAIL_LOOKUP_KEY | @base64d | length == 32' >/dev/null <<<"${config}"

jq -e '
  . as $root |
  (["api", "web", "rag", "voice-agent", "adaptor"] | all(.[]; $root.services[.].healthcheck.test != null))
  and (.services.api.ports | any(.published == "8000" and .target == 8000))
  and (.services.web.ports | any(.published == "3000" and .target == 3000))
  and (.services.adaptor.ports | any(.published == "3002" and .target == 3000))
' >/dev/null <<<"${config}"

jq -e '
  (.services.api.build.context | endswith("/api"))
  and .services.api.build.target == "runner"
  and (.services."api-migrator".build.context | endswith("/api"))
  and .services."api-migrator".build.target == "migrator"
  and (.services.web.build.context | endswith("/web"))
' >/dev/null <<<"${local_config}"

test -f config/api.env.example
test -f config/web.env.example
grep -Fq 'postgres_data:' compose.yml
grep -Fq 'redis_data:' compose.yml
! grep -Eiq 'keycloak|KC_' compose.yml .env.example config/api.env.example Makefile

printf 'compose contract: ok\n'
