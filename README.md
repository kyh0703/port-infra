# port-infra

로컬 개발에 필요한 상태 저장 서비스와 애플리케이션을 Docker Compose로 실행한다.
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
`rag`를 재생성할 때는 `postgres-app-init`을 먼저 실행한 뒤 동일한 RAG 이미지로 idempotent migration을 실행하고,
Compose 기동 시에도 `rag-migrator`가 성공적으로 완료된 뒤 RAG가 시작된다.
운영 중인 서비스 상태는 `make health`, 최근 로그는 `make logs`로 확인한다.
자동화와 운영 명령에서는 `docker compose down -v`를 사용하지 않는다.
Compose project name은 `infra`로 고정하므로 worktree가 달라도 같은 stack/volumes를 재사용한다.

Tailscale은 host-level client만 사용한다. Tailnet 내부에서는 `http://macbookpro:3000`으로
직접 접근하며 Serve, Funnel, 서비스별 Tailscale 컨테이너는 사용하지 않는다.

기존 Mac에 Tailscale Serve 또는 Funnel 설정이 남아 있으면 접근 host가 달라질 수 있다.
배포나 일반 health 확인에 자동 연결하지 않은 명시적 cleanup 명령으로 한 번 정리한다.

```bash
make tailscale-direct
tailscale serve status --json
tailscale funnel status --json
curl -I http://macbookpro:3000/
```

상태 JSON에 활성 web handler가 없어야 하며, 로그인은 반드시
`http://macbookpro:3000`에서 시작한다. HTTPS `*.ts.net` Serve 주소나
이전 `port-web` 장비 주소를 사용하지 않는다.

Colima 설정은 Docker 전용 4코어/6GB이며 Kubernetes를 설치하지 않는다.

기본 서비스:

| 서비스 | 주소 | 용도 |
| --- | --- | --- |
| PostgreSQL | `localhost:15432` | API와 RAG 데이터 |
| Redis | `localhost:6379` | 캐시·세션·Redis Streams |

`postgres-app-init`은 기존 volume의 app role과 `aggregator` NOLOGIN role을 idempotent하게
보정한 뒤 API migration과 Aggregator가 시작되도록 한다. 기존 데이터와 owner는 삭제하지 않는다.

새 로컬 DB의 개발 계정:

- 애플리케이션 로그인: `admin@overthinker.local` / `admin`

위 값은 deterministic dev 계정이다. 실제 환경에서는 반드시 교체하고 공유·운영 환경에서는
사용하지 않는다. 인증은 API가 관리하는 이메일/비밀번호와 HttpOnly 세션 쿠키를 사용한다.

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
Native auth, Web→API, API→RAG 연동도 별도 manual smoke로 확인한다. Aggregator는 distroless
이미지라 컨테이너 healthcheck 대신 host smoke (`3001/healthz`)를 사용한다.

로컬 Compose의 API는 `NODE_ENV=local`과
`AUTH_EMAIL_VERIFICATION_EXPOSE_DEBUG_CODE=true`를 사용한다. SMTP가 설정되지 않은 로컬에서
회원가입 이메일 인증 코드를 응답으로 반환하고 Web이 자동 입력할 수 있도록 하는 개발 전용 설정이다.
실제 환경에서는 SMTP를 설정하고 이 옵션을 반드시 `false`로 유지해야 하며,
인증 코드를 응답이나 로그로 노출하면 안 된다. `MAIL_HOST`, `MAIL_USER`, `MAIL_PASS`는
이 로컬 설정에 넣지 않는다.

Manual smoke checklist: native auth → Web→API→RAG→Voice→LiveKit registration 순서로
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
- 기본 scrape 대상: API `api:8000`, RAG `rag:8000`, Voice Agent `voice-agent:9091`, Aggregator `aggregator:3000`

중지:

```bash
make observability-down
```

Loki/Promtail은 포함하지 않는다. 애플리케이션 로그는 Compose 컨테이너 stdout에서 확인한다.

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
