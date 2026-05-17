# dubu-exec

`dubu-api` and `dubu-web` local execution workspace.

## Setup

```bash
git clone git@github.com:kyh0703/dubu-exec.git
cd dubu-exec
git submodule update --init --recursive
```

Create local environment file:

```bash
cp .env.example .env
```

`docker-compose.yml` loads `.env` into the API container. The web container receives only public frontend values from `.env`.

## Run

```bash
docker-compose up
```

Services:

- Web: http://localhost:3000
- API: http://localhost:3001/api/v1

The API uses SQLite at `/app/data/dubu.sqlite`, mounted from host `./data`, and runs MikroORM migrations before starting.

Set `OPENAI_API_KEY` in `.env` when testing realtime conversation features.

## Production Compose

Production Compose runs migration first against the same SQLite file mounted into the API container:

```bash
./scripts/deploy-prod-compose.sh
```

Order:

1. Build `api-migrate`, `api`, and `web` images.
2. Start Redis.
3. Run `api-migrate` once with `./data:/app/data` and `DB_PATH=/app/data/dubu.sqlite`.
4. Start `api` and `web` only after migration exits successfully.

Equivalent raw commands:

```bash
docker-compose -f docker-compose.prod.yml build api-migrate api web
docker-compose -f docker-compose.prod.yml up -d redis
docker-compose -f docker-compose.prod.yml run --rm api-migrate
docker-compose -f docker-compose.prod.yml up -d --no-deps api web
```

Keep `DB_PATH=/app/data/dubu.sqlite` in `.env`. If the `./data:/app/data` mount is removed, the app can use a different SQLite file than the migrated one.

## Stop

```bash
docker-compose down
```

To remove local data and dependency volumes:

```bash
docker-compose down -v
```

`./data` is a bind mount, so `down -v` does not delete the SQLite file.
