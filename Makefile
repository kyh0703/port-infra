COMPOSE ?= $(shell docker compose version >/dev/null 2>&1 && printf 'docker compose' || printf 'docker-compose')

ifneq (,$(wildcard .env))
include .env
export
endif

INFRA_SERVICES := postgres redis keycloak

.PHONY: infra-up infra-down infra-logs tools-up tools-down observability-up observability-down observability-logs db-ensure-user test up down ps

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
