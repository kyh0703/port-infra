#!/usr/bin/env bash
set -euo pipefail

# Test-only value, scoped to this config validation process; never a runtime default.
export INTERNAL_SERVER_KEY="compose-test-only-internal-key-0123456789"

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
telephony_config="$(${compose_command[@]} --env-file .env.example -f compose.yml --profile telephony config --format json)"

jq -e '
  . as $root |
  (.services.keycloak == null and .services."keycloak-db-init" == null)
  and (["postgres", "redis", "api", "web", "rag", "voice-agent", "aggregator", "adaptor", "livekit"] | all(.[]; $root.services[.] != null))
  and ([.services[] | has("build")] | any | not)
  and ([.services[]?.ports[]?.published] | any(. == "18080") | not)
' >/dev/null <<<"${config}"

jq -e '
  .services.openbao.image == "ghcr.io/openbao/openbao:2.6.2"
  and .services.openbao.command == ["server", "-config=/bao/config/openbao.hcl"]
  and .services.openbao.restart == "unless-stopped"
  and (.services.openbao.ports | any(.host_ip == "127.0.0.1" and (.published | tonumber) == 18200 and .target == 8200))
  and (.services.openbao.volumes | any(.type == "volume" and .source == "openbao_data" and .target == "/bao/data"))
  and (.services.openbao.volumes | any(.type == "volume" and .source == "openbao_audit" and .target == "/bao/audit"))
  and (.services.openbao.volumes | any(.target == "/bao/config/openbao.hcl" and .read_only == true))
  and (.services.openbao.volumes | any(.target == "/bao/tls/ca.crt" and .read_only == true))
  and (.services.openbao.volumes | any(.target == "/bao/tls/server.crt" and .read_only == true))
  and (.services.openbao.volumes | any(.target == "/bao/tls/server.key" and .read_only == true))
  and (.services.openbao.tmpfs | any(. == "/bao/tls-runtime"))
  and (.services.openbao.networks | has("secrets_internal"))
  and (.services.openbao.networks | has("openbao_host"))
  and (.services.api.networks | has("secrets_internal"))
  and (.services."api-migrator".networks | has("secrets_internal"))
  and (.services.api.networks | has("openbao_host") | not)
  and (.services."api-migrator".networks | has("openbao_host") | not)
  and .networks.secrets_internal.internal == true
  and .networks.openbao_host.internal != true
  and .services.openbao.environment.BAO_ADDR == "https://openbao:8200"
  and .services.openbao.environment.BAO_CACERT == "/bao/tls-runtime/ca.crt"
  and .services.api.environment.OPENBAO_ADDR == "https://openbao:8200"
  and .services.api.environment.OPENBAO_CA_CERT_FILE == "/run/openbao-ca/ca.crt"
  and .services.api.environment.OPENBAO_ROLE_ID_FILE == "/run/openbao/api-role-id"
  and .services.api.environment.OPENBAO_SECRET_ID_FILE == "/run/openbao/api-secret-id"
  and .services."api-migrator".environment.OPENBAO_ADDR == "https://openbao:8200"
  and .services."api-migrator".environment.OPENBAO_CA_CERT_FILE == "/run/openbao-ca/ca.crt"
  and .services."api-migrator".environment.OPENBAO_ROLE_ID_FILE == "/run/openbao/api-role-id"
  and .services."api-migrator".environment.OPENBAO_SECRET_ID_FILE == "/run/openbao/api-secret-id"
  and (.services.api.volumes | any(.target == "/run/openbao-ca/ca.crt" and .read_only == true))
  and (.services."api-migrator".volumes | any(.target == "/run/openbao-ca/ca.crt" and .read_only == true))
  and (.services.api.volumes | any(.type == "volume" and .source == "openbao_api_credentials" and .target == "/run/openbao" and .read_only == true))
  and (.services."api-migrator".volumes | any(.type == "volume" and .source == "openbao_api_credentials" and .target == "/run/openbao" and .read_only == true))
  and (.services.api.environment | has("OPENBAO_CACERT") | not)
  and (.services."api-migrator".environment | has("OPENBAO_CACERT") | not)
' >/dev/null <<<"${config}"

grep -Fq 'tls_disable     = false' openbao/config.hcl
grep -Fq 'tls_key_file    = "/bao/tls-runtime/server.key"' openbao/config.hcl
grep -Fq 'storage "raft"' openbao/config.hcl
grep -Fq 'file_path = "/bao/audit/audit.log"' openbao/config.hcl
grep -Fq 'chown -R openbao:openbao /bao/data /bao/audit' openbao/entrypoint.sh
grep -Fq 'cp /bao/tls/server.key /bao/tls-runtime/server.key' openbao/entrypoint.sh
grep -Fq 'chmod 600 /bao/tls-runtime/server.key' openbao/entrypoint.sh
grep -Fq 'su-exec openbao:openbao bao' openbao/entrypoint.sh
grep -Fq 'DNS:openbao' scripts/openbao-tls.sh
grep -Fq 'DNS:localhost' scripts/openbao-tls.sh
grep -Fq 'IP:127.0.0.1' scripts/openbao-tls.sh
grep -Fq '${OPENBAO_SNAPSHOT_DIR:-./data/openbao/snapshots}:/bao/snapshots' compose.yml
grep -Fq 'export OPENBAO_SNAPSHOT_DIR=' scripts/openbao.sh
grep -Fq 'ln -- "${temporary_output}" "${output_path}"' scripts/openbao.sh
! grep -Fq 'mv -- "${temporary_output}" "${output_path}"' scripts/openbao.sh
! grep -Eiq -- '(^|[[:space:]])-dev([[:space:]]|$)' openbao/config.hcl compose.yml scripts/openbao*.sh
if grep -Eq 'operator (init|unseal|raft snapshot save).*(KEY|TOKEN|key|token)=' scripts/openbao*.sh; then
  echo 'OpenBao secret material must not be placed in helper command arguments' >&2
  exit 1
fi

test -f openbao/policies/api-pii-envelope.hcl
grep -Fq 'path "secret/data/port/api/pii-envelope"' openbao/policies/api-pii-envelope.hcl
grep -Fq 'path "transit/decrypt/port-pii-kek"' openbao/policies/api-pii-envelope.hcl
grep -Fq 'path "secret/data/port/api/credential-envelope"' openbao/policies/api-pii-envelope.hcl
grep -Fq 'path "transit/decrypt/port-credential-kek"' openbao/policies/api-pii-envelope.hcl
grep -Fq 'path "auth/token/revoke-self"' openbao/policies/api-pii-envelope.hcl
grep -Fq 'capabilities = ["read"]' openbao/policies/api-pii-envelope.hcl
grep -Fq 'capabilities = ["update"]' openbao/policies/api-pii-envelope.hcl
! grep -Eq 'path "[^"]*\*|wildcard|export|datakey|write|create|delete|list|patch|sudo' openbao/policies/api-pii-envelope.hcl

jq -e '
  .services.api.image == "ghcr.io/kyh0703/port-api:dev"
  and .services.web.image == "ghcr.io/kyh0703/port-web:dev"
  and .services.rag.image == "ghcr.io/kyh0703/port-rag:dev"
  and .services."voice-agent".image == "ghcr.io/kyh0703/port-voice-agent:dev"
  and .services.aggregator.image == "ghcr.io/kyh0703/port-aggregator:dev"
  and .services.adaptor.image == "ghcr.io/kyh0703/port-adaptor:dev"
  and .services.livekit.image == "livekit/livekit-server:v1.13.6"
  and .services.livekit.command == ["--dev", "--bind", "0.0.0.0", "--redis-host", "redis:6379"]
  and ((.services.livekit.environment.LIVEKIT_CONFIG // "") | contains("enable_remote_unmute: true"))
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
  (["DATABASE_URL", "REDIS_URL", "RAG_URL", "RAG_RETRIEVAL_CAPABILITY_SECRET", "WEB_ORIGIN", "AUTH_PASSWORD_RESET_SECRET", "AUTH_EMAIL_VERIFICATION_SECRET", "AUTH_RATE_LIMIT_SECRET", "WEB_CHAT_RESUME_TOKEN_SECRET", "AUTH_SESSION_COOKIE_SECURE", "AUTH_SESSION_TTL_SECONDS", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"] | all(.[]; $root.services.api.environment[.] != null))
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
  and (.services.api.environment | has("USER_EMAIL_LOOKUP_KEY") | not)
  and (.services.api.environment | has("USER_PII_ENCRYPTION_KEY") | not)
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
! grep -Fq 'USER_EMAIL_LOOKUP_KEY=' config/api.env.example
! grep -Fq 'USER_PII_ENCRYPTION_KEY=' config/api.env.example
grep -Fq '${VOICE_AGENT_CONFIG_FILE:-./config/voice-agent.local.example.yaml}' compose.yml
grep -Fq 'postgres-app-init' compose.yml
grep -Fq 'aggregator' postgres/init/01-ensure-port-user.sh
! grep -Fq 'PLATFORM_HOSTNAME' .env.example
grep -Fq '"macbookpro:host-gateway"' compose.yml

jq -e '
  [.services.api.environment, .services."api-migrator".environment] |
  all(.[]; (has("VOICE_RUNTIME_CREDENTIAL_ENCRYPTION_KEY") or has("AGENT_TOOL_AUTH_ENCRYPTION_KEY")) | not)
' >/dev/null <<<"${config}"

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
  .services.api.image == "port-api-dev-runtime:local"
  and (.services.api.build.context | endswith("/api"))
  and .services.api.build.target == "deps"
  and .services.api.command == ["sh", "-lc", "pnpm install --store-dir /pnpm/store --frozen-lockfile && exec pnpm dev"]
  and .services.api.environment.NODE_ENV == "development"
  and .services.api.environment.CI == "true"
  and .services.api.environment.CHOKIDAR_USEPOLLING == "true"
  and (.services.api.volumes | any(.target == "/app"))
  and (.services.api.volumes | any(.target == "/app/node_modules" and .type == "volume"))
  and (.services.api.volumes | any(.target == "/pnpm/store" and .type == "volume" and .source == "api_pnpm_store"))
  and .services.web.image == "port-web-dev-runtime:local"
  and (.services.web.build.context | endswith("/web"))
  and .services.web.build.target == "deps"
  and .services.web.command == ["sh", "-lc", "pnpm install --store-dir /pnpm/store --frozen-lockfile && exec pnpm dev"]
  and .services.web.environment.NODE_ENV == "development"
  and .services.web.environment.CI == "true"
  and .services.web.environment.HOSTNAME == "0.0.0.0"
  and .services.web.environment.CHOKIDAR_USEPOLLING == "true"
  and .services.web.environment.WATCHPACK_POLLING == "true"
  and (.services.web.volumes | any(.target == "/app"))
  and (.services.web.volumes | any(.target == "/app/.next" and .type == "volume" and .source == "web_next_cache"))
  and (.services.web.volumes | any(.target == "/app/node_modules" and .type == "volume"))
  and (.services.web.volumes | any(.target == "/pnpm/store" and .type == "volume" and .source == "web_pnpm_store"))
  and (.services.postgres.volumes | any(.target == "/var/lib/postgresql/data"))
  and (.services.redis.volumes | any(.target == "/data"))
  and (.volumes.api_pnpm_store != null)
  and (.volumes.web_pnpm_store != null)
' >/dev/null <<<"${dev_config}"

jq -e '
  ([.services.api.volumes[]?.target, .services.web.volumes[]?.target] | all(. != "/var/lib/postgresql/data" and . != "/data"))
' >/dev/null <<<"${dev_config}"

jq -e '
  (.volumes.web_next_cache != null)
  and (.volumes.web_next_cache != .volumes.web_node_modules)
  and (.volumes.web_next_cache != .volumes.web_pnpm_store)
  and (.volumes.web_next_cache != .volumes.api_node_modules)
  and (.volumes.web_next_cache != .volumes.api_pnpm_store)
' >/dev/null <<<"${dev_config}"

jq -e '
  . as $root |
  ($root.services.api.volumes | map(select(.target == "/pnpm/store") | .source) | . == ["api_pnpm_store"])
  and ($root.services.web.volumes | map(select(.target == "/pnpm/store") | .source) | . == ["web_pnpm_store"])
  and (($root.services.api.volumes | map(select(.target == "/pnpm/store") | .source)) != ($root.services.web.volumes | map(select(.target == "/pnpm/store") | .source)))
' >/dev/null <<<"${dev_config}"

printf 'compose dev contract: ok\n'

jq -e '
  .services.livekit.profiles == null
  and (.services.livekit.command | index("--redis-host") != null)
  and .services.livekit.depends_on.redis.condition == "service_healthy"
  and .services."livekit-sip".profiles == ["telephony"]
  and .services."livekit-sip".image == "livekit/sip:v1.7.0"
  and .services."livekit-sip".environment.LIVEKIT_API_KEY == "devkey"
  and .services."livekit-sip".environment.LIVEKIT_API_SECRET == "secret"
  and .services."livekit-sip".environment.LIVEKIT_URL == "ws://livekit:7880"
  and .services."livekit-sip".environment.REDIS_ADDRESS == "redis:6379"
  and (.services."livekit-sip".ports | any(.published == "18090" and .target == 8080))
  and (.services."livekit-sip".ports | any(.published == "15090" and .target == 5060))
  and (.services."livekit-sip".ports | any(.published == "15000" and .target == 10000))
  and .services.asterisk.profiles == ["telephony"]
  and .services.asterisk.image == "andrius/asterisk:22-alpine"
  and (.services.asterisk.volumes | any(.target == "/etc/asterisk/modules.conf" and .read_only == true))
  and (.services.asterisk.ports | any(.published == "15060" and .target == 5060))
  and (.services.asterisk.ports | any(.published == "15100" and .target == 10000))
  and .services."livekit-cli".profiles == ["telephony"]
  and .services."livekit-cli".image == "livekit/livekit-cli:v2.16.3"
  and (.services."livekit-cli".volumes | any(.target == "/sip" and .read_only == true))
  and (.services."livekit-sip".ports | all(.host_ip == "127.0.0.1"))
  and (.services.asterisk.ports | all(.host_ip == "127.0.0.1"))
  and .services."livekit-sip".healthcheck.test[0] == "CMD"
  and .services."livekit-sip".healthcheck.test[1] == "bash"
  and (.services."livekit-sip".healthcheck.test[3] | contains("GET / HTTP/1.0"))
  and (.services."livekit-sip".healthcheck.test[3] | contains("status") and contains("200"))
  and (.services."livekit-sip" | has("network_mode") | not)
  and (.services.asterisk | has("network_mode") | not)
' >/dev/null <<<"${telephony_config}"

jq -e '(.services."livekit-sip" == null and .services.asterisk == null and .services."livekit-cli" == null)' >/dev/null <<<"${config}"
test -f sip/inbound-trunk.json
test -f sip/outbound-trunk.json
test -f sip/dispatch-rule.json
test -x scripts/sip-provision.sh
jq -e '.dispatch_rule.roomConfig.agents[0].agentName == "voice-agent"' sip/dispatch-rule.json >/dev/null
jq -e '.trunk.numbers == ["2000"]' sip/inbound-trunk.json >/dev/null
jq -e '.trunk.numbers == ["600"]' sip/outbound-trunk.json >/dev/null
grep -Fq 'same => n,Echo()' asterisk/extensions.conf
grep -Fq 'PJSIP/${EXTEN:1}@livekit' asterisk/extensions.conf
grep -Fq 'noload => chan_alsa.so' asterisk/modules.conf
printf 'telephony compose contract: ok\n'

# The key belongs to server callers only, including migration settings validation.
jq -e '
  .services.api.environment.INTERNAL_SERVER_KEY as $key |
  ($key | length) >= 32
  and .services.rag.environment.INTERNAL_SERVER_KEY == $key
  and .services["voice-agent"].environment.INTERNAL_SERVER_KEY == $key
  and .services["api-migrator"].environment.INTERNAL_SERVER_KEY == $key
  and .services["rag-migrator"].environment.INTERNAL_SERVER_KEY == $key
  and (.services.web.environment | has("INTERNAL_SERVER_KEY") | not)
  and (.services.adaptor.environment | has("INTERNAL_SERVER_KEY") | not)
' >/dev/null <<<"${config}"
