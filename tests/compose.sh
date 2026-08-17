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
  . as $root |
  .services.postgres
  and .services.redis
  and .services.keycloak
  and (["api", "web", "rag", "voice-agent", "aggregator", "adaptor", "livekit"] | all(.[]; $root.services[.] != null))
  and ([.services[] | has("build")] | any | not)
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
  and .services.api.depends_on.keycloak.condition == "service_healthy"
  and .services.api.depends_on."api-migrator".condition == "service_completed_successfully"
  and .services."api-migrator".depends_on."postgres-app-init".condition == "service_completed_successfully"
  and .services."api-migrator".image == "ghcr.io/kyh0703/port-api:migrator"
  and .services."api-migrator".restart == "no"
  and .services."api-migrator".depends_on.postgres.condition == "service_healthy"
  and .services."postgres-app-init".image == "pgvector/pgvector:pg17"
  and .services."postgres-app-init".restart == "no"
  and .services."postgres-app-init".depends_on.postgres.condition == "service_healthy"
  and (.services."postgres-app-init".volumes | any(.target == "/usr/local/bin/ensure-port-user" and .read_only == true))
  and .services."api-migrator".environment.DATABASE_URL == .services.api.environment.DATABASE_URL
  and .services.rag.depends_on.postgres.condition == "service_healthy"
  and .services.aggregator.depends_on.postgres.condition == "service_healthy"
  and .services.aggregator.depends_on."postgres-app-init".condition == "service_completed_successfully"
  and .services.aggregator.depends_on.redis.condition == "service_healthy"
  and .services."voice-agent".depends_on.api.condition == "service_started"
  and .services."voice-agent".depends_on.rag.condition == "service_healthy"
  and .services.adaptor.depends_on.api.condition == "service_started"
  and .services.api.depends_on.livekit.condition == "service_started"
  and .services."voice-agent".depends_on.livekit.condition == "service_started"
  and .services.adaptor.environment.PUBLIC_BASE_URL == "http://macbookpro:3002"
  and (.services.adaptor.environment.ACCESS_TOKEN_IDENTITY_URL == null)
' >/dev/null <<<"${config}"

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
' >/dev/null <<<"${config}"

jq -e '
  . as $root |
  (["DATABASE_URL", "REDIS_URL", "RAG_URL", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET", "KEYCLOAK_REDIRECT_URI", "KEYCLOAK_TRANSACTION_SECRET", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "VOICE_RUNTIME_CREDENTIAL_ENCRYPTION_KEY", "USER_PII_ENCRYPTION_KEY", "KEYCLOAK_SESSION_COOKIE_SECURE"] | all(.[]; $root.services.api.environment[.] != null))
  and .services.api.environment.KEYCLOAK_ISSUER_URL == "http://macbookpro:18080/realms/overthinker"
  and .services.api.environment.WEB_ORIGIN == "http://macbookpro:3000"
  and .services.api.environment.KEYCLOAK_REDIRECT_URI == "http://macbookpro:3000/api/v1/auth/callback"
  and .services.api.environment.LIVEKIT_URL == "ws://macbookpro:7880"
  and .services.api.environment.LIVEKIT_API_SECRET == "secret"
  and .services.api.environment.VOICE_RUNTIME_CREDENTIAL_ENCRYPTION_KEY == "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="
  and .services.api.environment.USER_PII_ENCRYPTION_KEY == "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI="
  and .services."voice-agent".environment.LIVEKIT_URL == "ws://macbookpro:7880"
  and .services."voice-agent".environment.LIVEKIT_API_SECRET == "secret"
  and .services."voice-agent".environment.LIVEKIT_URL != null
  and .services."voice-agent".environment.LIVEKIT_API_KEY != null
  and .services."voice-agent".environment.LIVEKIT_API_SECRET != null
' >/dev/null <<<"${config}"

jq -e '
  .services."voice-agent".ports[0].published == "19091"
  and .services."voice-agent".ports[0].target == 9091
  and .services."voice-agent".healthcheck.test[0] == "CMD"
  and (.services.api.ports | any(.published == "8000" and .target == 8000))
  and (.services.web.ports | any(.published == "3000" and .target == 3000))
  and (.services.keycloak.ports | any(.published == "18080" and .target == 8080))
  and (.services.adaptor.ports | any(.published == "3002" and .target == 3000))
' >/dev/null <<<"${config}"

jq -e '
  .services.keycloak.environment.KC_HOSTNAME == "http://macbookpro:18080"
  and (.services.api.extra_hosts | any(. == "macbookpro=host-gateway"))
  and (.services.api.extra_hosts | any(. == "host.docker.internal=host-gateway"))
  and (.services."voice-agent".extra_hosts | any(. == "macbookpro=host-gateway"))
  and (.services."voice-agent".extra_hosts | any(. == "host.docker.internal=host-gateway"))
  and (.services."voice-agent".volumes | any(.target == "/app/config/local.yaml" and .read_only == true))
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
grep -Fq 'KC_HOSTNAME: http://macbookpro:18080' compose.yml
grep -Fq '"macbookpro:host-gateway"' compose.yml

api_voice_key=$(jq -r '.services.api.environment.VOICE_RUNTIME_CREDENTIAL_ENCRYPTION_KEY' <<<"${config}")
api_pii_key=$(jq -r '.services.api.environment.USER_PII_ENCRYPTION_KEY' <<<"${config}")
[[ "$(printf '%s' "$api_voice_key" | openssl base64 -d -A | wc -c | tr -d ' ')" == "32" ]]
[[ "$(printf '%s' "$api_pii_key" | openssl base64 -d -A | wc -c | tr -d ' ')" == "32" ]]

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
  and .internationalizationEnabled == true
  and .supportedLocales == ["ko"]
  and .defaultLocale == "ko"
  and (.clients | any(.clientId == "overthinker-api"))
  and (.clients | any(
    .clientId == "overthinker-api-bff"
    and .secret == "overthinker-local-bff-secret"
    and .rootUrl == "http://macbookpro:3000"
    and .adminUrl == "http://macbookpro:3000"
    and .redirectUris == ["http://macbookpro:3000/api/v1/auth/callback"]
    and .webOrigins == ["http://macbookpro:3000"]
    and .attributes."post.logout.redirect.uris" == "http://macbookpro:3000/*"
    and (.protocolMappers | any(.config."included.client.audience" == "overthinker-api"))
  ))
  and (.roles.client."overthinker-api" | map(.name) | sort == ["admin", "user"])
  and (.users | any(.username == "admin@overthinker.local"))
' keycloak/import/overthinker-realm.json >/dev/null

grep -Fxq 'locales=ko' keycloak/theme/login/theme.properties

grep -Fq 'link.href = "http://macbookpro:3000/";' \
  keycloak/theme/login/resources/js/overthinker.js

printf 'compose contract: ok\n'
