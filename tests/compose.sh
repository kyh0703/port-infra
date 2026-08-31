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
test ! -e compose.local.yml
dev_config="$(${compose_command[@]} --env-file .env.example -f compose.yml -f compose.dev.yml config --format json)"

jq -e '
  . as $root |
  (.services.keycloak == null and .services."keycloak-db-init" == null)
  and (["postgres", "redis", "api", "web", "rag", "voice-agent", "aggregator", "adaptor", "livekit"] | all(.[]; $root.services[.] != null))
  and ([.services[] | has("build")] | any | not)
  and ([.services[]?.ports[]?.published] | any(. == "18080") | not)
' >/dev/null <<<"${config}"

jq -e '
  .services.api.image == "ghcr.io/kyh0703/port-api:dev"
  and .services.web.image == "ghcr.io/kyh0703/port-web:dev"
  and .services.rag.image == "ghcr.io/kyh0703/port-rag:dev"
  and .services."voice-agent".image == "ghcr.io/kyh0703/port-voice-agent:dev"
  and .services.aggregator.image == "ghcr.io/kyh0703/port-aggregator:dev"
  and .services.adaptor.image == "ghcr.io/kyh0703/port-adaptor:dev"
  and .services.livekit.image == "livekit/livekit-server:latest"
  and .services.livekit.command == ["--dev", "--bind", "0.0.0.0"]
' >/dev/null <<<"${config}"

jq -e '
  .services.api.depends_on.postgres.condition == "service_healthy"
  and .services.api.depends_on.redis.condition == "service_healthy"
  and (.services.api.depends_on.keycloak == null)
  and .services.api.depends_on."api-migrator".condition == "service_completed_successfully"
  and .services."api-migrator".image == "ghcr.io/kyh0703/port-api:migrator"
  and .services."api-migrator".restart == "no"
  and .services."postgres-app-init".image == "pgvector/pgvector:pg17"
  and .services."postgres-app-init".restart == "no"
  and .services."api-migrator".depends_on.postgres.condition == "service_healthy"
  and .services."api-migrator".depends_on."postgres-app-init".condition == "service_completed_successfully"
  and .services."postgres-app-init".depends_on.postgres.condition == "service_healthy"
  and (.services."postgres-app-init".volumes | any(.target == "/usr/local/bin/ensure-port-user" and .read_only == true))
  and .services."api-migrator".environment.DATABASE_URL == .services.api.environment.DATABASE_URL
  and .services.rag.depends_on.postgres.condition == "service_healthy"
  and .services.rag.depends_on."rag-migrator".condition == "service_completed_successfully"
  and .services."rag-migrator".image == "ghcr.io/kyh0703/port-rag:dev"
  and .services."rag-migrator".restart == "no"
  and .services."rag-migrator".depends_on.postgres.condition == "service_healthy"
  and .services."rag-migrator".depends_on."postgres-app-init".condition == "service_completed_successfully"
  and (.services."rag-migrator".environment.DATABASE_URL != null and .services."rag-migrator".environment.DATABASE_URL != "")
  and .services."rag-migrator".environment.DATABASE_URL == .services.rag.environment.DATABASE_URL
  and .services."rag-migrator".command == ["uv", "run", "--no-sync", "alembic", "upgrade", "head"]
  and .services.aggregator.depends_on.postgres.condition == "service_healthy"
  and .services.aggregator.depends_on."postgres-app-init".condition == "service_completed_successfully"
  and .services.aggregator.depends_on.redis.condition == "service_healthy"
  and .services."voice-agent".depends_on.api.condition == "service_started"
  and .services."voice-agent".depends_on.rag.condition == "service_healthy"
  and .services."voice-agent".depends_on.livekit.condition == "service_started"
  and .services.adaptor.depends_on.api.condition == "service_started"
  and .services.api.depends_on.livekit.condition == "service_started"
  and .services.adaptor.environment.PUBLIC_BASE_URL == "http://macbookpro:3002"
  and (.services.adaptor.environment.ACCESS_TOKEN_IDENTITY_URL == null)
' >/dev/null <<<"${config}"

jq -e '
  . as $root |
  (["DATABASE_URL", "REDIS_URL", "RAG_URL", "RAG_RETRIEVAL_CAPABILITY_SECRET", "WEB_ORIGIN", "AUTH_PASSWORD_RESET_SECRET", "AUTH_EMAIL_VERIFICATION_SECRET", "AUTH_RATE_LIMIT_SECRET", "USER_EMAIL_LOOKUP_KEY", "WEB_CHAT_RESUME_TOKEN_SECRET", "AUTH_SESSION_COOKIE_SECURE", "AUTH_SESSION_TTL_SECONDS", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "VOICE_RUNTIME_CREDENTIAL_ENCRYPTION_KEY", "USER_PII_ENCRYPTION_KEY"] | all(.[]; $root.services.api.environment[.] != null))
  and ([.services.api.environment | keys[] | select(test("KEYCLOAK|KC_"))] | length == 0)
  and .services.api.environment.WEB_ORIGIN == "http://macbookpro:3000"
  and .services.api.environment.LIVEKIT_URL == "ws://macbookpro:7880"
  and .services.api.environment.LIVEKIT_API_SECRET == "secret"
  and .services.api.environment.NODE_ENV == "local"
  and (.services.api.environment | has("AUTH_EMAIL_VERIFICATION_EXPOSE_DEBUG_CODE") | not)
  and (["MAIL_HOST", "MAIL_USER", "MAIL_PASS"] | all(.[]; ($root.services.api.environment[.] // "") == ""))
  and .services."voice-agent".environment.LIVEKIT_URL == "ws://macbookpro:7880"
  and .services."voice-agent".environment.LIVEKIT_API_SECRET == "secret"
  and .services."voice-agent".environment.LIVEKIT_URL != null
  and .services."voice-agent".environment.LIVEKIT_API_KEY != null
  and .services."voice-agent".environment.LIVEKIT_API_SECRET != null
' >/dev/null <<<"${config}"

jq -e '.services.api.environment.USER_EMAIL_LOOKUP_KEY | @base64d | length == 32' >/dev/null <<<"${config}"

jq -e '
  .services.livekit.restart == "unless-stopped"
  and (.services.livekit.ports | any(.published == "7880" and .target == 7880 and .protocol == "tcp"))
  and (.services.livekit.ports | any(.published == "7881" and .target == 7881 and .protocol == "tcp"))
  and (.services.livekit.ports | any(.published == "7882" and .target == 7882 and .protocol == "udp"))
  and .services.api.environment.LIVEKIT_URL == "ws://macbookpro:7880"
  and .services."voice-agent".environment.LIVEKIT_URL == "ws://macbookpro:7880"
' >/dev/null <<<"${config}"

jq -e '
  . as $root |
  (["api", "web", "rag", "voice-agent", "adaptor"] | all(.[]; $root.services[.].healthcheck.test != null))
  and (.services."voice-agent".volumes | any(.target == "/app/config/local.yaml" and .read_only == true))
  and (.services.api.ports | any(.published == "8000" and .target == 8000))
  and (.services.web.ports | any(.published == "3000" and .target == 3000))
  and (.services.adaptor.ports | any(.published == "3002" and .target == 3000))
' >/dev/null <<<"${config}"

jq -e '
  (.services.api.extra_hosts | any(. == "macbookpro=host-gateway"))
  and (.services.api.extra_hosts | any(. == "host.docker.internal=host-gateway"))
  and (.services."voice-agent".extra_hosts | any(. == "macbookpro=host-gateway"))
  and (.services."voice-agent".extra_hosts | any(. == "host.docker.internal=host-gateway"))
' >/dev/null <<<"${config}"

test -f config/api.env.example
test -f config/web.env.example
test -f config/rag.env.example
test -f config/voice-agent.env.example
test -f config/aggregator.env.example
test -f config/adaptor.env.example
test -f config/voice-agent.local.example.yaml
grep -Fq '${VOICE_AGENT_CONFIG_FILE:-./config/voice-agent.local.example.yaml}' compose.yml
grep -Fq 'postgres-app-init' compose.yml
grep -Fq 'aggregator' postgres/init/01-ensure-port-user.sh
! grep -Fq 'PLATFORM_HOSTNAME' .env.example
grep -Fq '"macbookpro:host-gateway"' compose.yml

api_voice_key=$(jq -r '.services.api.environment.VOICE_RUNTIME_CREDENTIAL_ENCRYPTION_KEY' <<<"${config}")
api_pii_key=$(jq -r '.services.api.environment.USER_PII_ENCRYPTION_KEY' <<<"${config}")
[[ "$(printf '%s' "${api_voice_key}" | openssl base64 -d -A | wc -c | tr -d ' ')" == "32" ]]
[[ "$(printf '%s' "${api_pii_key}" | openssl base64 -d -A | wc -c | tr -d ' ')" == "32" ]]

jq -e '
  .services.redis.ports[0].published == "6379"
  and .services.pgadmin.profiles == ["tools"]
' >/dev/null <<<"${config}"

jq -e '
  .services.prometheus.profiles == ["observability"]
  and .services.grafana.profiles == ["observability"]
  and (.services.grafana.volumes | any(.target == "/etc/grafana/provisioning"))
' >/dev/null <<<"${config}"

grep -Fq 'postgres_data:' compose.yml
grep -Fq 'pgadmin_data:' compose.yml
grep -Fq 'redis_data:' compose.yml
grep -Fq 'prometheus_data:' compose.yml
grep -Fq 'grafana_data:' compose.yml
! grep -Eiq 'keycloak|KC_' compose.yml .env.example config/api.env.example Makefile
! grep -Eiq '^(MAIL_HOST|MAIL_USER|MAIL_PASS)=[^[:space:]]+' config/api.env.example

printf 'compose contract: ok\n'

jq -e '
  (.services.api.build.context | endswith("/api"))
  and .services.api.build.target == "deps"
  and .services.api.command == ["pnpm", "dev"]
  and .services.api.environment.NODE_ENV == "development"
  and (.services.api.volumes | any(.target == "/app"))
  and (.services.web.build.context | endswith("/web"))
  and .services.web.build.target == "deps"
  and .services.web.command == ["pnpm", "dev"]
  and .services.web.environment.NODE_ENV == "development"
  and (.services.web.volumes | any(.target == "/app"))
  and (.services.postgres.volumes | any(.target == "/var/lib/postgresql/data"))
  and (.services.redis.volumes | any(.target == "/data"))
' >/dev/null <<<"${dev_config}"

jq -e '
  ([.services.api.volumes[]?.target, .services.web.volumes[]?.target] | all(. != "/var/lib/postgresql/data" and . != "/data"))
' >/dev/null <<<"${dev_config}"

printf 'compose dev contract: ok\n'
