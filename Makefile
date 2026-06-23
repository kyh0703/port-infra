COMPOSE ?= docker compose

INFRA_SERVICES := postgres redis pgadmin
APP_SERVICES := api media web

.PHONY: infra-up infra-down infra-logs app-up app-down up down ps

infra-up:
	$(COMPOSE) --profile infra up -d $(INFRA_SERVICES)

infra-down:
	$(COMPOSE) stop $(INFRA_SERVICES)

infra-logs:
	$(COMPOSE) logs -f $(INFRA_SERVICES)

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
