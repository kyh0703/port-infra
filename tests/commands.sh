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
grep -Fq 'stack/volumes' README.md
grep -Fq 'idempotent' README.md
grep -Fq '$(COMPOSE) pull $(PULL_SERVICES)' Makefile
grep -Fq '$(COMPOSE) up -d --no-deps --force-recreate $(CHANGED_SERVICES)' Makefile
grep -Fq '$(COMPOSE) ps' Makefile
grep -Fq '$(COMPOSE) logs --tail=100 $(LOG_SERVICES)' Makefile
grep -Fq 'RAG_PORT ?= 8001' Makefile
grep -Fq 'AGGREGATOR_PORT ?= 3001' Makefile
grep -Fq 'ADAPTOR_PORT := 3002' Makefile
grep -Fq '"rag|http://127.0.0.1:$(RAG_PORT)/healthz"' Makefile
grep -Fq '$(COMPOSE) run --rm --no-deps api-migrator' Makefile
grep -Fq '$(COMPOSE) run --rm --no-deps rag-migrator' Makefile
grep -Fq '$(COMPOSE) run --rm --no-deps postgres-app-init' Makefile
grep -Fq '"aggregator|http://127.0.0.1:$(AGGREGATOR_PORT)/healthz"' Makefile
grep -Fq '"adaptor|http://127.0.0.1:$(ADAPTOR_PORT)/healthz"' Makefile
grep -Fq 'curl -fsS "$${url}"' Makefile
grep -Fq 'HEALTH_RETRIES ?= 30' Makefile
grep -Fq 'HEALTH_INTERVAL ?= 2' Makefile
grep -Fq '$(MAKE) health' Makefile
grep -Fq 'filter-out $(COMPOSE_ALLOWLIST)' Makefile
grep -Fq 'CHANGED_SERVICES="api web"' README.md
grep -Fq 'bash tests/commands.sh' Makefile
grep -Fq 'openbao-app-role:' Makefile
grep -Fq 'python3 scripts/openbao-app-role.py' Makefile

# Inspect the executable plan, not the Makefile spelling: this operation must
# target only our HTTPS listener, never another listener or the global state.
ingress_plan=$(make -n tailscale-ingress COMPOSE=true INGRESS_PORT=18088)
[[ "$ingress_plan" == *'--https=8443'* ]]
[[ "$ingress_plan" == *'http://127.0.0.1:18088'* ]]
if [[ "$ingress_plan" == *'reset'* || "$ingress_plan" == *'funnel'* || "$ingress_plan" == *'--https=443'* ]]; then
  echo 'ingress activation must not modify global Tailscale state or Serve 443' >&2
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
