필요 조건: Docker Compose 또는 Podman Compose

```bash
git clone --recurse-submodules git@github.com:kyh0703/port-infra.git
cd port-infra
```

submodule 없이 clone했다면:

```bash
git submodule update --init --recursive
```

실행:

```bash
cp .env.example .env
make infra-up  # postgres, redis, pgadmin
```

전체를 한 번에 띄우려면:

```bash
make up
```

기존 PostgreSQL volume에서 `port` 계정 인증이 실패하면:

```bash
make db-ensure-user
```

인프라만 실행:

```bash
docker compose --profile infra up
```

서비스:

- PostgreSQL: localhost:15432
- pgAdmin: http://localhost:5050
- Redis: localhost:16379
