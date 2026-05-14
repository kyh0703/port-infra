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

The API uses SQLite at `/app/data/dubu.sqlite` inside the `api_data` Docker volume and runs MikroORM migrations before starting.

Set `OPENAI_API_KEY` in `.env` when testing realtime conversation features.

## Stop

```bash
docker-compose down
```

To remove local data and dependency volumes:

```bash
docker-compose down -v
```
