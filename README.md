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
docker compose --profile app up
```

인프라만 실행:

```bash
docker compose --profile infra up
```

인프라가 이미 실행 중일 때 앱만 실행:

```bash
docker compose --profile app up --no-deps api media web
```

서비스:

- API: http://localhost:3001/api/v1/health
- Media: http://localhost:8080/api/v1/health
- Web: http://localhost:3000
- Redis: localhost:6379
