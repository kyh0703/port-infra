COMPOSE ?= $(shell docker compose version >/dev/null 2>&1 && printf 'docker compose' || printf 'docker-compose')
COMPOSE_PROJECT_NAME ?= infra
override COMPOSE_PROJECT_NAME := infra
export COMPOSE_PROJECT_NAME

ifneq (,$(wildcard .env))
include .env
export
endif

INFRA_SERVICES := postgres redis keycloak
APP_SERVICES := api web rag voice-agent aggregator adaptor
PULL_SERVICES := $(APP_SERVICES) api-migrator livekit
LOG_SERVICES := $(APP_SERVICES) livekit
CHANGED_SERVICES ?= api
API_PORT ?= 8000
WEB_PORT := 3000
RAG_PORT ?= 8001
VOICE_AGENT_METRICS_PORT ?= 19091
LIVEKIT_PORT ?= 7880
AGGREGATOR_PORT ?= 3001
ADAPTOR_PORT := 3002
HEALTH_RETRIES ?= 30
HEALTH_INTERVAL ?= 2
COMPOSE_ALLOWLIST := api web rag voice-agent aggregator adaptor
INVALID_CHANGED_SERVICES := $(filter-out $(COMPOSE_ALLOWLIST),$(CHANGED_SERVICES))

ifneq ($(strip $(INVALID_CHANGED_SERVICES)),)
$(error unsupported CHANGED_SERVICES: $(INVALID_CHANGED_SERVICES))
endif
ifeq ($(strip $(CHANGED_SERVICES)),)
$(error CHANGED_SERVICES must not be empty)
endif

.PHONY: colima-start pull deploy recreate health logs infra-up infra-down infra-logs tools-up tools-down observability-up observability-down observability-logs db-ensure-user test up down ps

colima-start:
	colima start --vm-type vz --runtime docker --cpus 4 --memory 6 --disk 60

pull:
	$(COMPOSE) pull $(PULL_SERVICES)

deploy: colima-start pull
	$(COMPOSE) up -d
	$(MAKE) health

recreate:
	$(COMPOSE) pull $(RECREATE_PULL_SERVICES)
	$(MIGRATOR_RECREATE)
	$(COMPOSE) up -d --no-deps --force-recreate $(CHANGED_SERVICES)

health:
	@set -eu; \
	attempt=1; \
	while ! nc -z 127.0.0.1 $(LIVEKIT_PORT) >/dev/null 2>&1; do \
		if [ "$${attempt}" -ge "$(HEALTH_RETRIES)" ]; then \
			echo "health check failed: livekit tcp 127.0.0.1:$(LIVEKIT_PORT)" >&2; exit 1; \
		fi; \
		attempt=$$((attempt + 1)); sleep "$(HEALTH_INTERVAL)"; \
	done; \
	for check in \
		"api|http://127.0.0.1:$(API_PORT)/api/v1/health" \
		"web|http://127.0.0.1:$(WEB_PORT)/" \
		"rag|http://127.0.0.1:$(RAG_PORT)/healthz" \
		"voice-agent|http://127.0.0.1:$(VOICE_AGENT_METRICS_PORT)/metrics" \
		"aggregator|http://127.0.0.1:$(AGGREGATOR_PORT)/healthz" \
		"adaptor|http://127.0.0.1:$(ADAPTOR_PORT)/healthz"; do \
		name=$${check%%|*}; url=$${check#*|}; attempt=1; \
		while ! curl -fsS "$${url}" >/dev/null 2>&1; do \
			if [ "$${attempt}" -ge "$(HEALTH_RETRIES)" ]; then \
				echo "health check failed: $${name} $${url}" >&2; exit 1; \
			fi; \
			attempt=$$((attempt + 1)); sleep "$(HEALTH_INTERVAL)"; \
		done; \
	done; \
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs --tail=100 $(LOG_SERVICES)

infra-up:
	$(COMPOSE) up -d $(INFRA_SERVICES)

infra-down:
	$(COMPOSE) stop $(INFRA_SERVICES)

infra-logs:
	$(COMPOSE) logs -f $(INFRA_SERVICES)

tools-up:
	$(COMPOSE) --profile tools up -d pgadmin

tools-down:
	$(COMPOSE) --profile tools stop pgadmin

observability-up:
	$(COMPOSE) --profile observability up -d prometheus grafana

observability-down:
	$(COMPOSE) --profile observability stop prometheus grafana

observability-logs:
	$(COMPOSE) --profile observability logs -f prometheus grafana

db-ensure-user:
	$(COMPOSE) up -d postgres
	$(COMPOSE) exec -T --user postgres postgres sh /docker-entrypoint-initdb.d/01-ensure-port-user.sh

test:
	COMPOSE="$(COMPOSE)" bash tests/compose.sh

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps
RECREATE_PULL_SERVICES := $(CHANGED_SERVICES) $(if $(filter api,$(CHANGED_SERVICES)),api-migrator,)
MIGRATOR_RECREATE := $(if $(filter api,$(CHANGED_SERVICES)),$(COMPOSE) run --rm --no-deps api-migrator,)
