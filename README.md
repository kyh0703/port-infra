# port-infra

로컬 개발에 필요한 상태 저장 서비스, 인증 서비스, 여섯 애플리케이션을 Docker Compose로 실행한다.
애플리케이션은 각 저장소가 GHCR에 발행한 `:dev` 이미지를 사용한다.

## 실행

현재 Mac 기준 Docker 런타임은 Kubernetes 없는 Colima를 사용한다.

최초 한 번:

```bash
brew install colima docker docker-compose
colima start --vm-type vz --runtime docker --cpus 4 --memory 6 --disk 60
```

이후 인프라 실행:

```bash
cp .env.example .env
make infra-up
```

전체 플랫폼 이미지 운영

여섯 애플리케이션은 sibling 저장소가 GHCR에 발행한 `:dev` 이미지를 사용한다.
Colima 시작, 이미지 pull, 전체 서비스 재생성은 다음처럼 실행한다.

```bash
make deploy
make pull
make health
make logs
make recreate CHANGED_SERVICES="api web"
```

`recreate`는 지정한 서비스만 pull/recreate하며 named volume을 건드리지 않는다.
운영 중인 서비스 상태는 `make health`, 최근 로그는 `make logs`로 확인한다.
자동화와 운영 명령에서는 `docker compose down -v`를 사용하지 않는다.
Compose project name은 `infra`로 고정하므로 worktree가 달라도 같은 stack/volumes를 재사용한다.

Tailscale은 host-level client만 사용한다. Tailnet 내부에서는 `http://macbookpro:3000`으로
직접 접근하며 Serve, Funnel, 서비스별 Tailscale 컨테이너는 사용하지 않는다.

Colima 설정은 Docker 전용 4코어/6GB이며 Kubernetes를 설치하지 않는다.

기본 서비스:

| 서비스 | 주소 | 용도 |
| --- | --- | --- |
| PostgreSQL | `localhost:15432` | API, RAG, Keycloak 데이터 |
| Redis | `localhost:6379` | 캐시와 Redis Streams |
| Keycloak | `http://macbookpro:18080` | `overthinker` realm 인증 |

Keycloak은 기존 PostgreSQL의 `keycloak` 데이터베이스를 그대로 사용한다. 시작 전
`keycloak-db-init`이 전용 계정과 데이터베이스를 보장하고 `.env`의 로컬 비밀번호로
계정을 갱신한다. realm, 사용자, client 데이터는 삭제하지 않는다.
`postgres-app-init`은 기존 volume의 app role과 `aggregator` NOLOGIN role을 idempotent하게
보정한 뒤 API migration과 Aggregator가 시작되도록 한다. 기존 데이터와 owner는 삭제하지 않는다.

Keycloak issuer:

```text
http://macbookpro:18080/realms/overthinker
```

API 로컬 환경의 `KEYCLOAK_ISSUER_URL`도 이 주소를 사용한다.

새 로컬 DB의 개발 계정:

- 애플리케이션 로그인: `admin@overthinker.local` / `admin`
- Keycloak 관리 콘솔: `admin` / `admin`
- API BFF client secret: `overthinker-local-bff-secret`

위 값은 deterministic dev keys인 로컬 bootstrap 전용이다. 실제 환경에서는 반드시 교체하고
공유·운영 환경에서는 사용하지 않는다.

## Compose 서비스 포트와 사전 조건

앱은 모두 Compose 컨테이너로 실행한다. 호스트 포트와 컨테이너 내부 포트는 다음과 같다.

| 서비스 | 호스트 포트 | 컨테이너 포트 |
| --- | ---: | ---: |
| Web | `3000` | `3000` |
| Adaptor | `3002` | `3000` |
| API HTTP | `8000` | `8000` |
| API gRPC | `8080` | `8080` |
| RAG HTTP | `8001` | `8000` |
| Voice Agent metrics | `19091` | `9091` |
| Aggregator | `3001` | `3000` |

Compose의 `livekit` 서비스(`livekit/livekit-server:latest`)가 LiveKit dev server를 실행한다.
API와 Voice Agent는
`config/api.env`와 `config/voice-agent.env`의 `LIVEKIT_URL`인 `ws://macbookpro:7880`을
사용한다. Compose에서는 각각 `API_ENV_FILE`과 `VOICE_AGENT_ENV_FILE`로 이 파일을 지정한다.

커스텀 설정은 example 파일을 local 파일로 복사한 뒤 root `.env`의
`API_ENV_FILE`과 `VOICE_AGENT_ENV_FILE`을 복사한 경로로 지정한다.

LiveKit dev server의 기본 개발 credential은 `devkey`/`secret`이며 운영 credential로
사용하지 않는다.

로컬 Compose에서는 adaptor의 PAT identity integration을 비활성화한다. identity endpoint는
HTTPS endpoint를 제공하는 환경에서만 local env로 opt-in하며, `PUBLIC_BASE_URL`은
`http://macbookpro:3002`로 고정한다.

`make health`는 각 컨테이너의 liveness smoke와 LiveKit TCP 포트 검사를 수행한다. Voice Agent의 metrics endpoint는
process liveness만 보장하며 LiveKit registration은 logs와 별도 manual smoke로 확인한다.
Keycloak callback, Web→API, API→RAG 연동도 별도 manual smoke로 확인한다. Aggregator는 distroless
이미지라 컨테이너 healthcheck 대신 host smoke (`3001/healthz`)를 사용한다.

Manual smoke checklist: Keycloak callback → Web→API→RAG→Voice→LiveKit registration 순서로
로그인, API 호출, RAG 요청, Voice bootstrap, LiveKit room 접속을 확인한다.

## Tailscale 접속

macOS Tailscale 앱에서 로그인하고 MagicDNS의 호스트명이 `macbookpro`인지 확인한다.

```bash
tailscale login
tailscale status
tailscale ping macbookpro
ssh user@macbookpro
```

macOS Remote Login을 켠 뒤 표준 macOS SSH 서버에 `ssh user@macbookpro`로 접속한다.
macOS에서는 Tailscale SSH 서버를 별도로 설정하지 않는다. Funnel과 public 노출도 사용하지 않는다.

## 선택 관리 도구

pgAdmin은 기본 실행에서 제외된다.

```bash
make tools-up
```

- pgAdmin: http://localhost:5050

## 선택 관측 스택

Prometheus와 Grafana는 기본 실행에서 제외된다. 메트릭을 볼 때만 실행한다.

```bash
make observability-up
```

- Prometheus: http://localhost:19090
- Grafana: http://localhost:13000
- 기본 scrape 대상: API `api:8000`, RAG `rag:8000`, Voice Agent `voice-agent:9091`, Aggregator `aggregator:3000`, Keycloak

중지:

```bash
make observability-down
```

Loki/Promtail은 포함하지 않는다. 애플리케이션 로그는 Compose 컨테이너 stdout에서 확인한다.

## Keycloak realm import와 테마

- 기존 DB가 있으면 DB 상태가 우선이며 별도 import가 필요 없다.
- 기존 Keycloak DB에서는 realm import 파일을 바꿔도 callback URL이 자동 갱신되지 않는다. `overthinker-api-bff` client의 callback URL을 수동 확인하고 필요하면 client를 재생성한다.
- 새 DB에 realm을 넣을 때 export JSON을 `keycloak/import/`에 둔다.
- realm export에는 사용자 정보가 포함될 수 있어 `keycloak/import/*`는 Git에서 제외된다.
- 현재 Kubernetes에서 사용하던 `overthinker` 로그인 테마는 `keycloak/theme/`에 포함한다.

## 관리 명령

```bash
make infra-logs
make ps
make test
make infra-down
make down
```

기존 PostgreSQL volume에서 `port` 계정 인증이 실패하면:

```bash
make db-ensure-user
```

전체 volume 삭제는 realm과 로컬 데이터를 제거하므로 `docker compose down -v`를 사용하지 않는다.
