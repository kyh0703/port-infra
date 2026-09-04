#!/usr/bin/env bash
set -euo pipefail

grep -Fq 'colima start --vm-type vz --runtime docker' Makefile
grep -Fq 'COMPOSE_PROJECT_NAME ?= infra' Makefile
grep -Fq 'export COMPOSE_PROJECT_NAME' Makefile
grep -Fq 'INFRA_SERVICES := postgres redis' Makefile
! grep -Eiq 'keycloak|KC_' Makefile
! grep -Fq 'API_BUILD_CONTEXT' .env.example
! grep -Fq 'WEB_BUILD_CONTEXT' .env.example
test ! -e compose.local.yml
grep -Fq 'compose.dev.yml' Makefile
grep -Fq 'dev-up:' Makefile
grep -Fq 'dev-logs:' Makefile
grep -Fq 'dev-stop:' Makefile
! grep -Fq 'PORT_WORKSPACE_ROOT' Makefile compose.dev.yml
grep -Fq -- 'DEV_COMPOSE := $(COMPOSE) -f compose.yml -f compose.dev.yml' Makefile
grep -Fq 'context: ../api' compose.dev.yml
grep -Fq 'context: ../web' compose.dev.yml
grep -Fq '../api:/app' compose.dev.yml
grep -Fq '../web:/app' compose.dev.yml
grep -Fq '$(DEV_COMPOSE) up -d --build --no-deps api web' Makefile
grep -Fq '$(DEV_COMPOSE) stop api web' Makefile
grep -Fq 'stack/volumes' README.md
grep -Fq 'idempotent' README.md
grep -Fq 'PULL_SERVICES := $(APP_SERVICES) api-migrator rag-migrator livekit' Makefile
grep -Fq 'livekit' Makefile
grep -Fq '$(COMPOSE) pull $(PULL_SERVICES)' Makefile
grep -Fq '$(COMPOSE) up -d --no-deps --force-recreate $(CHANGED_SERVICES)' Makefile
grep -Fq '$(COMPOSE) ps' Makefile
grep -Fq '$(COMPOSE) logs --tail=100 $(LOG_SERVICES)' Makefile
grep -Fq 'API_PORT ?= 8000' Makefile
grep -Fq 'WEB_PORT := 3000' Makefile
grep -Fq 'RAG_PORT ?= 8001' Makefile
grep -Fq 'VOICE_AGENT_METRICS_PORT ?= 19091' Makefile
grep -Fq 'LIVEKIT_PORT ?= 7880' Makefile
grep -Fq 'LIVEKIT_SIP_HEALTH_PORT ?= 18090' Makefile
grep -Fq 'noload => res_phoneprov.so' asterisk/modules.conf
grep -Fq 'noload => res_hep_pjsip.so' asterisk/modules.conf
grep -Fq 'noload => app_stasis.so' asterisk/modules.conf
grep -Fq 'noload => cdr_custom.so' asterisk/modules.conf
grep -Fq 'AGGREGATOR_PORT ?= 3001' Makefile
grep -Fq 'ADAPTOR_PORT := 3002' Makefile
grep -Fq '"api|http://127.0.0.1:$(API_PORT)/api/v1/health"' Makefile
grep -Fq '"web|http://127.0.0.1:$(WEB_PORT)/"' Makefile
grep -Fq '"rag|http://127.0.0.1:$(RAG_PORT)/healthz"' Makefile
grep -Fq '"voice-agent|http://127.0.0.1:$(VOICE_AGENT_METRICS_PORT)/metrics"' Makefile
grep -Fq '$(COMPOSE) run --rm --no-deps api-migrator' Makefile
grep -Fq '$(COMPOSE) run --rm --no-deps rag-migrator' Makefile
grep -Fq '$(COMPOSE) run --rm --no-deps postgres-app-init' Makefile
grep -Fq '"aggregator|http://127.0.0.1:$(AGGREGATOR_PORT)/healthz"' Makefile
grep -Fq '"adaptor|http://127.0.0.1:$(ADAPTOR_PORT)/healthz"' Makefile
grep -Fq 'curl -fsS "$${url}"' Makefile
grep -Fq 'nc -z 127.0.0.1 $(LIVEKIT_PORT)' Makefile
grep -Fq 'HEALTH_RETRIES ?= 30' Makefile
grep -Fq 'HEALTH_INTERVAL ?= 2' Makefile
grep -Fq '$(MAKE) health' Makefile
grep -Fq 'filter-out $(COMPOSE_ALLOWLIST)' Makefile
grep -Fq 'CHANGED_SERVICES="api web"' README.md
grep -Fq 'bash tests/commands.sh' Makefile
grep -Fq 'telephony-up:' Makefile
grep -Fq -- '--profile telephony up -d --wait livekit-sip asterisk' Makefile
grep -Fq 'telephony-provision:' Makefile
grep -Fq 'telephony-health:' Makefile
grep -Fq 'telephony-logs:' Makefile
grep -Fq 'telephony-down:' Makefile
grep -Fq 'scripts/sip-provision.sh' Makefile
grep -Fq 'telephony' README.md
grep -Fq 'sip-provision.sh' README.md
grep -Fq 'sipTrunkId' scripts/sip-provision.sh
grep -Fq 'sipDispatchRuleId' scripts/sip-provision.sh
grep -Fq 'wait_for_livekit' scripts/sip-provision.sh
grep -Fq 'SIP_PROVISION_RETRIES' scripts/sip-provision.sh
grep -Fq "pjsip show endpoint livekit" Makefile
! grep -Fq 'nc -z 127.0.0.1 $(ASTERISK_SIP_PORT)' Makefile

grep -Fq 'tailscale-direct' Makefile
grep -Fq 'tailscale serve reset' Makefile
grep -Fq 'tailscale funnel reset' Makefile
grep -Fq 'tailscale serve status --json' Makefile
grep -Fq 'tailscale funnel status --json' Makefile
if grep -Eq '^(deploy|recreate|health):.*tailscale-direct' Makefile; then
  echo 'tailscale cleanup must remain explicit' >&2
  exit 1
fi

assert_tailscale_command_forms() {
  local file=$1
  local line service action
  while IFS= read -r line; do
    [[ "$line" =~ tailscale[[:space:]]+(serve|funnel)[[:space:]]+([^[:space:]]+) ]] || continue
    service=${BASH_REMATCH[1]}
    action=${BASH_REMATCH[2]}
    if [[ "$action" != 'reset' && "$action" != 'status' ]]; then
      echo "tailscale $service activation is forbidden: $line" >&2
      return 1
    fi
  done < "$file"
}

assert_tailscale_command_forms Makefile
assert_tailscale_command_forms README.md
for forbidden in \
  'tailscale serve localhost:3000' \
  'tailscale serve http+insecure://localhost:3000' \
  'tailscale serve unix:/tmp/web.sock' \
  'tailscale funnel localhost:3000'; do
  if assert_tailscale_command_forms <(printf '%s\n' "$forbidden"); then
    echo "forbidden Tailscale activation was accepted: $forbidden" >&2
    exit 1
  fi
done

grep -Fq 'make deploy' README.md
grep -Fq 'make pull' README.md
grep -Fq 'make recreate CHANGED_SERVICES="api web"' README.md
grep -Fq 'make health' README.md
grep -Fq 'make logs' README.md
grep -Fq 'http://macbookpro:3000' README.md
grep -Fq 'livekit/livekit-server:latest' README.md
grep -Fq '7880' README.md
if grep -Fq 'livekit-server --dev' README.md || grep -Fq 'host.docker.internal:7880' README.md; then
  echo 'external LiveKit prerequisite remains documented' >&2
  exit 1
fi
grep -Fq 'config/api.env' README.md
grep -Fq 'config/voice-agent.env' README.md
grep -Fq 'API_ENV_FILE' README.md
grep -Fq 'VOICE_AGENT_ENV_FILE' README.md
grep -Fq 'LiveKit registration' README.md
grep -Fq 'tailscale login' README.md
grep -Fq 'tailscale status' README.md
grep -Fq 'tailscale ping macbookpro' README.md
grep -Fq 'ssh user@macbookpro' README.md
grep -Fq 'make tailscale-direct' README.md
grep -Fq 'tailscale serve status --json' README.md
grep -Fq 'tailscale funnel status --json' README.md
grep -Fq 'tailscale status' README.md
grep -Fq 'PAT identity integration' README.md
grep -Fq 'HTTPS endpoint' README.md
grep -Fq 'liveness smoke' README.md
grep -Fq 'host smoke' README.md
grep -Fq 'LiveKit TCP' README.md
grep -Fq 'deterministic dev 계정' README.md
grep -Fq 'Web→API→RAG→Voice→LiveKit' README.md
if grep -Fq 'Voice Agent readiness' README.md || grep -Fq '18081' README.md; then
  echo 'obsolete Voice Agent readiness port documentation is present' >&2
  exit 1
fi
if grep -Fq 'VOICE_AGENT_HEALTH_PORT' .env.example; then
  echo 'VOICE_AGENT_HEALTH_PORT must not be present' >&2
  exit 1
fi
grep -Fq 'PUBLIC_BASE_URL=http://macbookpro:3002' config/adaptor.env.example
! grep -Fq 'ACCESS_TOKEN_IDENTITY_URL' config/adaptor.env.example
grep -Fq 'api:8000' prometheus/prometheus.yml
grep -Fq 'rag:8000' prometheus/prometheus.yml
grep -Fq 'voice-agent:9091' prometheus/prometheus.yml
grep -Fq 'aggregator:3000' prometheus/prometheus.yml
if grep -Eq '^[[:space:]]*[^#].*docker(-compose)?( compose)? down -v' Makefile; then
  echo 'destructive down -v target is forbidden' >&2
  exit 1
fi
if make -n recreate CHANGED_SERVICES='api;touch /tmp/compose-command-injection' >/dev/null 2>&1; then
  echo 'malicious CHANGED_SERVICES was accepted' >&2
  exit 1
fi
if make -n recreate CHANGED_SERVICES= >/dev/null 2>&1; then
  echo 'empty CHANGED_SERVICES was accepted' >&2
  exit 1
fi
recreate_plan=$(make -n recreate CHANGED_SERVICES=api)
pull_line=$(printf '%s\n' "$recreate_plan" | sed -n '1p')
migrate_line=$(printf '%s\n' "$recreate_plan" | sed -n '2p')
[[ "$pull_line" == *'pull api api-migrator'* ]]
[[ "$migrate_line" == *'run --rm --no-deps api-migrator'* ]]
recreate_plan=$(make -n recreate CHANGED_SERVICES=rag)
pull_line=$(printf '%s\n' "$recreate_plan" | sed -n '1p')
migrate_line=$(printf '%s\n' "$recreate_plan" | sed -n '2p')
[[ "$pull_line" == *'pull rag rag-migrator'* ]]
[[ "$migrate_line" == *'run --rm --no-deps postgres-app-init'*'run --rm --no-deps rag-migrator'* ]]
recreate_plan=$(make -n recreate CHANGED_SERVICES='api rag')
pull_line=$(printf '%s\n' "$recreate_plan" | sed -n '1p')
migrate_line=$(printf '%s\n' "$recreate_plan" | sed -n '2p')
[[ "$pull_line" == *'pull api rag api-migrator rag-migrator'* ]]
[[ "$migrate_line" == *'run --rm --no-deps api-migrator'*'run --rm --no-deps postgres-app-init'*'run --rm --no-deps rag-migrator'* ]]
printf 'commands contract: ok\n'
