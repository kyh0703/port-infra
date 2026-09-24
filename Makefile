COMPOSE ?= $(shell docker compose version >/dev/null 2>&1 && printf 'docker compose' || printf 'docker-compose')
COMPOSE_PROJECT_NAME ?= infra
override COMPOSE_PROJECT_NAME := infra
export COMPOSE_PROJECT_NAME

DEV_COMPOSE := $(COMPOSE) -f compose.yml -f compose.dev.yml

ifneq (,$(wildcard .env))
include .env
export
endif

INFRA_SERVICES := postgres redis
APP_SERVICES := api web rag voice-agent aggregator adaptor
PULL_SERVICES := $(APP_SERVICES) api-migrator rag-migrator livekit
LOG_SERVICES := $(APP_SERVICES) livekit
CHANGED_SERVICES ?= api
API_PORT ?= 8000
WEB_PORT := 3000
RAG_PORT ?= 8001
VOICE_AGENT_METRICS_PORT ?= 19091
LIVEKIT_PORT ?= 7880
LIVEKIT_SIP_HEALTH_PORT ?= 18090
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

.PHONY: colima-start pull deploy recreate health logs infra-up infra-down infra-logs openbao-tls openbao-up openbao-status openbao-init openbao-unseal openbao-snapshot openbao-app-role openbao-down openbao-logs tools-up tools-down observability-up observability-down observability-logs telephony-up telephony-provision telephony-health telephony-logs telephony-down db-ensure-user tailscale-direct dev-up dev-logs dev-stop test up down ps

colima-start:
	colima start --vm-type vz --runtime docker --cpus 4 --memory 6 --disk 60

pull:
	$(COMPOSE) pull $(PULL_SERVICES) openbao

deploy: colima-start openbao-tls pull
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

dev-up:
	$(DEV_COMPOSE) up -d --build --no-deps api web

dev-logs:
	$(DEV_COMPOSE) logs -f api web

dev-stop:
	$(DEV_COMPOSE) stop api web

openbao-tls:
	bash scripts/openbao-tls.sh
	mkdir -p data/openbao/snapshots
	chmod 700 data/openbao/snapshots

openbao-up: openbao-tls
	bash scripts/openbao.sh up

openbao-status:
	bash scripts/openbao.sh status

openbao-init:
	bash scripts/openbao.sh init

openbao-unseal:
	bash scripts/openbao.sh unseal

openbao-snapshot:
	bash scripts/openbao.sh snapshot

openbao-app-role:
	python3 scripts/openbao-app-role.py

.PHONY: openbao-keychain-prepare openbao-keychain-init openbao-auto-install openbao-auto-stop
openbao-keychain-prepare:
	python3 scripts/openbao-autounseal.py prepare

openbao-keychain-init:
	python3 scripts/openbao-autounseal.py setup-key

openbao-auto-install:
	python3 scripts/openbao-autounseal.py install

openbao-auto-stop:
	python3 scripts/openbao-autounseal.py stop

openbao-down:
	bash scripts/openbao.sh down

openbao-logs:
	bash scripts/openbao.sh logs

infra-up: openbao-tls
	$(COMPOSE) up -d $(INFRA_SERVICES) openbao

infra-down:
	$(COMPOSE) stop $(INFRA_SERVICES) openbao

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

telephony-up:
	$(COMPOSE) --profile telephony up -d --wait livekit-sip asterisk

telephony-provision: telephony-up
	COMPOSE="$(COMPOSE)" bash scripts/sip-provision.sh

telephony-health:
	@curl -fsS "http://127.0.0.1:$(LIVEKIT_SIP_HEALTH_PORT)" >/dev/null
	@$(COMPOSE) --profile telephony exec -T asterisk asterisk -rx 'pjsip show endpoint livekit' | grep -q 'Endpoint:  livekit'
	@$(COMPOSE) --profile telephony ps livekit-sip asterisk

telephony-logs:
	$(COMPOSE) --profile telephony logs --tail=100 livekit-sip asterisk

telephony-down:
	$(COMPOSE) --profile telephony stop livekit-sip asterisk livekit-cli

.PHONY: pjsua-build pjsua-setup pjsua-caller pjsua-agent
pjsua-build:
	docker --context "$${DOCKER_CONTEXT:-colima}" build -t port-pjsua:2.17 pjsua

pjsua-setup:
	python3 scripts/pjsua-setup.py
	$(COMPOSE) --profile telephony up -d --no-deps --wait asterisk
	$(COMPOSE) --profile telephony exec -T asterisk asterisk -rx 'pjsip reload'

pjsua-caller:
	bash scripts/pjsua.sh caller

pjsua-agent:
	bash scripts/pjsua.sh agent --auto-answer=200

db-ensure-user:
	$(COMPOSE) up -d postgres
	$(COMPOSE) exec -T --user postgres postgres sh /docker-entrypoint-initdb.d/01-ensure-port-user.sh

tailscale-direct:
	tailscale serve reset
	tailscale funnel reset
	tailscale serve status --json
	tailscale funnel status --json

test:
	python3 tests/test_internal_key_init.py
	python3 -m unittest discover -s tests -p 'test_openbao*.py'
	COMPOSE="$(COMPOSE)" bash tests/compose.sh
	bash tests/commands.sh

up: openbao-tls
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps
RECREATE_PULL_SERVICES := $(CHANGED_SERVICES) $(if $(filter api,$(CHANGED_SERVICES)),api-migrator,) $(if $(filter rag,$(CHANGED_SERVICES)),rag-migrator,)
MIGRATOR_RECREATE := $(strip $(if $(filter api,$(CHANGED_SERVICES)),$(COMPOSE) run --rm --no-deps api-migrator$(if $(filter rag,$(CHANGED_SERVICES)), && ,),)$(if $(filter rag,$(CHANGED_SERVICES)),$(COMPOSE) run --rm --no-deps postgres-app-init && $(COMPOSE) run --rm --no-deps rag-migrator,))
