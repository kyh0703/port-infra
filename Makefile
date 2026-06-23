COMPOSE ?= docker compose

ifneq (,$(wildcard .env))
include .env
export
endif

INFRA_SERVICES := postgres redis pgadmin
APP_SERVICES := api media web

.PHONY: infra-up infra-down infra-logs db-ensure-user app-up app-down up down ps

infra-up:
	$(COMPOSE) --profile infra up -d $(INFRA_SERVICES)

infra-down:
	$(COMPOSE) stop $(INFRA_SERVICES)

infra-logs:
	$(COMPOSE) logs -f $(INFRA_SERVICES)

db-ensure-user:
	$(COMPOSE) --profile infra up -d postgres
	$(COMPOSE) exec -T --user postgres postgres sh /docker-entrypoint-initdb.d/01-ensure-port-user.sh

app-up:
	$(COMPOSE) --profile app up -d $(APP_SERVICES)

app-down:
	$(COMPOSE) stop $(APP_SERVICES)

up:
	$(COMPOSE) --profile infra --profile app up -d

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps
