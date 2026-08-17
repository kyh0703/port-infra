#!/usr/bin/env bash
set -euo pipefail

grep -Fq 'colima start --vm-type vz --runtime docker' Makefile
grep -Fq 'COMPOSE_PROJECT_NAME ?= infra' Makefile
grep -Fq 'export COMPOSE_PROJECT_NAME' Makefile
grep -Fq 'stack/volumes' README.md
grep -Fq 'idempotent' README.md
grep -Fq 'PULL_SERVICES := $(APP_SERVICES) api-migrator livekit' Makefile
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
grep -Fq 'AGGREGATOR_PORT ?= 3001' Makefile
grep -Fq 'ADAPTOR_PORT := 3002' Makefile
grep -Fq '"api|http://127.0.0.1:$(API_PORT)/api/v1/health"' Makefile
grep -Fq '"web|http://127.0.0.1:$(WEB_PORT)/"' Makefile
grep -Fq '"rag|http://127.0.0.1:$(RAG_PORT)/healthz"' Makefile
grep -Fq '"voice-agent|http://127.0.0.1:$(VOICE_AGENT_METRICS_PORT)/metrics"' Makefile
grep -Fq '$(COMPOSE) run --rm --no-deps api-migrator' Makefile
grep -Fq '"aggregator|http://127.0.0.1:$(AGGREGATOR_PORT)/healthz"' Makefile
grep -Fq '"adaptor|http://127.0.0.1:$(ADAPTOR_PORT)/healthz"' Makefile
grep -Fq 'curl -fsS "$${url}"' Makefile
grep -Fq 'nc -z 127.0.0.1 $(LIVEKIT_PORT)' Makefile
grep -Fq 'HEALTH_RETRIES ?= 30' Makefile
grep -Fq 'HEALTH_INTERVAL ?= 2' Makefile
grep -Fq '$(MAKE) health' Makefile
grep -Fq 'filter-out $(COMPOSE_ALLOWLIST)' Makefile
grep -Fq 'CHANGED_SERVICES="api web"' README.md

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
grep -Fq 'tailscale status' README.md
grep -Fq 'callback URL' README.md
grep -Fq 'PAT identity integration' README.md
grep -Fq 'HTTPS endpoint' README.md
grep -Fq 'liveness smoke' README.md
grep -Fq 'host smoke' README.md
grep -Fq 'LiveKit TCP' README.md
grep -Fq 'deterministic dev keys' README.md
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
if grep -Fq 'tailscale serve' Makefile; then
  echo 'tailscale serve must not be present in Makefile' >&2
  exit 1
fi
if grep -Fq 'tailscale serve' README.md; then
  echo 'tailscale serve must not be present in README.md' >&2
  exit 1
fi

printf 'commands contract: ok\n'
