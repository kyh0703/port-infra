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
APP_SERVICES := ingress api web rag voice-agent aggregator adaptor
PULL_SERVICES := $(APP_SERVICES) api-migrator rag-migrator
LOG_SERVICES := $(APP_SERVICES)
CHANGED_SERVICES ?= api
INGRESS_PORT ?= 8088
RAG_PORT ?= 8001
AGGREGATOR_PORT ?= 3001
ADAPTOR_PORT := 3002
HEALTH_RETRIES ?= 30
HEALTH_INTERVAL ?= 2
COMPOSE_ALLOWLIST := ingress api web rag voice-agent aggregator adaptor
INVALID_CHANGED_SERVICES := $(filter-out $(COMPOSE_ALLOWLIST),$(CHANGED_SERVICES))

ifneq ($(strip $(INVALID_CHANGED_SERVICES)),)
$(error unsupported CHANGED_SERVICES: $(INVALID_CHANGED_SERVICES))
endif
ifeq ($(strip $(CHANGED_SERVICES)),)
$(error CHANGED_SERVICES must not be empty)
endif

.PHONY: colima-start pull deploy recreate health logs infra-up infra-down infra-logs openbao-tls openbao-up openbao-status openbao-init openbao-unseal openbao-snapshot openbao-app-role openbao-down openbao-logs tools-up tools-down observability-up observability-down observability-logs db-ensure-user tailscale-ingress dev-up dev-logs dev-stop test test-ingress up down ps

colima-start:
	colima start --vm-type vz --runtime docker --cpus 4 --memory 6 --disk 60

pull:
	$(COMPOSE) pull $(PULL_SERVICES) openbao

deploy: colima-start openbao-tls pull
	$(COMPOSE) up -d
	$(MAKE) health

# Each pull of a moving tag (:dev, :migrator) leaves the previous image dangling; prune it so the Colima disk does not fill.
recreate:
	$(COMPOSE) pull $(RECREATE_PULL_SERVICES)
	$(MIGRATOR_RECREATE)
	$(COMPOSE) up -d --no-deps --force-recreate $(CHANGED_SERVICES)
	docker image prune -f

health:
	@set -eu; \
	attempt=1; \
	while ! $(COMPOSE) exec -T voice-agent node -e "fetch('http://127.0.0.1:8081/').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))" >/dev/null 2>&1; do \
		if [ "$${attempt}" -ge "$(HEALTH_RETRIES)" ]; then \
			echo "health check failed: voice-agent LiveKit registration readiness" >&2; exit 1; \
		fi; \
		attempt=$$((attempt + 1)); sleep "$(HEALTH_INTERVAL)"; \
	done; \
	for check in \
		"ingress-api|http://127.0.0.1:$(INGRESS_PORT)/api/v1/health" \
		"ingress-web|http://127.0.0.1:$(INGRESS_PORT)/" \
		"rag|http://127.0.0.1:$(RAG_PORT)/healthz" \
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
	$(DEV_COMPOSE) up -d --build --no-deps api web ingress

dev-logs:
	$(DEV_COMPOSE) logs -f api web ingress

dev-stop:
	$(DEV_COMPOSE) stop ingress api web

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

db-ensure-user:
	$(COMPOSE) up -d postgres
	$(COMPOSE) exec -T --user postgres postgres sh /docker-entrypoint-initdb.d/01-ensure-port-user.sh

tailscale-ingress:
	tailscale serve --bg --https=8443 http://127.0.0.1:$(INGRESS_PORT)
	tailscale serve status --json

test:
	python3 tests/test_internal_key_init.py
	python3 -m unittest discover -s tests -p 'test_openbao*.py'
	python3 -m unittest discover -s tests -p 'test_runtime*.py'
	COMPOSE="$(COMPOSE)" bash tests/compose.sh
	bash tests/commands.sh
	$(MAKE) test-ingress

test-ingress:
	python3 tests/test_ingress.py

up: openbao-tls
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps
RECREATE_PULL_SERVICES := $(CHANGED_SERVICES) $(if $(filter api,$(CHANGED_SERVICES)),api-migrator,) $(if $(filter rag,$(CHANGED_SERVICES)),rag-migrator,)
MIGRATOR_RECREATE := $(strip $(if $(filter api,$(CHANGED_SERVICES)),$(COMPOSE) run --rm --no-deps api-migrator$(if $(filter rag,$(CHANGED_SERVICES)), && ,),)$(if $(filter rag,$(CHANGED_SERVICES)),$(COMPOSE) run --rm --no-deps postgres-app-init && $(COMPOSE) run --rm --no-deps rag-migrator,))
