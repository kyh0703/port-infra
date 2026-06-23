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
make app-up    # api, media, web
```

전체를 한 번에 띄우려면:

```bash
make up
```

서비스:

- API: http://localhost:3001/api/v1/health
- Media: http://localhost:8080/api/v1/health
- Web: http://localhost:3000
- PostgreSQL: localhost:5432
- pgAdmin: http://localhost:5050
- Redis: localhost:16379
