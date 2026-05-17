#!/usr/bin/env sh
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"

docker-compose -f "$COMPOSE_FILE" build api-migrate api web
docker-compose -f "$COMPOSE_FILE" up -d redis
docker-compose -f "$COMPOSE_FILE" run --rm api-migrate
docker-compose -f "$COMPOSE_FILE" up -d --no-deps api web
