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
python3 scripts/init-internal-key.py
make infra-up
```

`INTERNAL_SERVER_KEY`는 API·Worker·RAG 사이에서만 사용하는 공통 키다. 초기화 스크립트는
`.env`의 다른 설정과 이미 등록한 키를 유지하며, 새 키의 값은 출력하지 않는다.
파일 권한은 소유자만 읽고 쓸 수 있는 `0600`으로 설정한다. 키를 Git, 브라우저 환경변수,
공개 Web 프록시나 외부 도구 헤더에 넣지 않는다. 기존 환경에 적용할 때도 초기화 후
Worker → API → RAG 순서로 갱신하고 각 서비스의 health를 확인한다.

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

## 소스 마운트 개발 모드

API와 Web을 로컬 소스로 실행하고 파일 변경 시 watch/hot reload를 사용하려면 다음 명령을 실행한다.
`compose.dev.yml`은 기본 `compose.yml`을 덮어쓰며, infra 루트 기준 sibling 저장소의 `../api`와 `../web`을
각 컨테이너 `/app`에 마운트하고 dev 전용 로컬 이미지로 `deps` stage를 다시 빌드한다.
컨테이너 시작 시 `pnpm install --frozen-lockfile`을 먼저 실행하므로 lockfile 변경 후에도
stale `node_modules` volume을 재사용하지 않는다.

```bash
make dev-up
make dev-logs
make dev-stop
```

`dev-stop`은 `api`와 `web` 컨테이너만 중지하며 PostgreSQL·Redis 등 named volume은 삭제하지 않는다.
`make dev-up` 실행 전에는 기본 Compose로 PostgreSQL, Redis와 같은 infra 의존 서비스가 이미
실행 중이어야 하며, 이 명령은 API와 Web만 build/recreate한다.
개발 모드는 primary infra 저장소 루트에서 실행해야 하며,
API는 `NODE_ENV=development`, Web은 `pnpm dev`로 실행된다. 기본 이미지 기반 실행으로
돌아가려면 `make up`을 사용한다.

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

## OpenBao 비밀 저장소

OpenBao는 공식 `ghcr.io/openbao/openbao:2.6.2` 이미지로 실행하며, 단일 노드 Raft 데이터와 JSON audit log를 각각 named volume에 저장한다.
서버는 `https://openbao:8200`으로 `secrets_internal` 네트워크에만 연결되고, 호스트에는 `https://127.0.0.1:18200`으로만 노출된다.
`api`와 `api-migrator`만 이 내부 네트워크에 함께 연결된다. 호스트 포트 전달을 위한 `openbao_host` 네트워크에는 OpenBao만 연결된다.

최초 실행과 수동 unseal:

```bash
make openbao-up
make openbao-status
OPENBAO_INIT_OUTPUT=/secure/operator/openbao-init-20260918.txt make openbao-init
make openbao-unseal
make openbao-unseal
make openbao-unseal
make openbao-status
```

`OPENBAO_INIT_OUTPUT`은 저장소와 `data/openbao/` 같은 서버 마운트 밖의 새 절대 경로여야 한다. `make openbao-init`은 owner-only 파일에 초기화 응답을 저장하고 터미널에는 경로만 출력한다. 출력된 unseal share와 root token은 서로 분리된 승인된 오프라인 보관소에 직접 보관하고, 저장소·Compose 환경변수·명령 인자에 넣지 않는다. `make openbao-unseal`은 매번 입력을 숨겨서 받으며 share를 파일에 저장하지 않는다. OpenBao는 sealed 상태로 시작하므로 같은 호스트에 자동 unseal 키를 함께 두지 않는다.
초기화 명령이 비정상 종료되거나 최종 출력 파일을 안전하게 발행하지 못하면 helper는 임시 응답을 삭제하지 않고 owner-only 복구 파일로 남긴다. 터미널에는 복구 파일 경로만 안내되므로, 해당 파일을 승인된 오프라인 보관 절차로 먼저 보관하고 OpenBao 상태와 기존 출력 파일을 확인한 뒤 원래 `OPENBAO_INIT_OUTPUT` 경로를 덮어쓰지 않는 방식으로 후속 처리한다. 복구 파일이 확보되기 전에는 `make openbao-init`을 다시 실행하지 않는다.

TLS 준비 파일은 `data/openbao/tls/`에 생성되고 Git에서 무시된다. 인증서는 `openbao`, `localhost`, `127.0.0.1` SAN을 포함하며, 재실행 시 기존 유효한 파일을 보존한다. OpenBao는 read-only TLS 소스를 컨테이너 tmpfs에 복사하고 `openbao` UID로 읽는다. API 컨테이너가 사용할 CA 경로는 `/run/openbao-ca/ca.crt`, 내부 주소는 `https://openbao:8200`으로 고정한다.
API role-id/secret-id 파일(`/run/openbao/api-role-id`, `/run/openbao/api-secret-id`)은 후속 operator provisioning 단계에서만 생성·마운트한다. 이 기반 구성에는 자격증명 파일을 만들거나 보관하지 않는다.
API와 `api-migrator`는 `openbao_api_credentials` named volume을 `/run/openbao`에 read-only로 마운트하고, CA는 `/run/openbao-ca/ca.crt`에 별도 read-only mount한다. 후속 provisioning은 role-id/secret-id를 UID 1001, mode `0400`으로 기록한다. AppRole 정책 원본은 [openbao/policies/api-pii-envelope.hcl](openbao/policies/api-pii-envelope.hcl)이며 필요한 envelope 경로만 허용한다.

수동 policy 등록은 unseal된 operator 세션에서 수행한다. policy 본문은 표준 입력으로 전달하며 token을 명령 인자나 저장소에 넣지 않는다.

```bash
docker compose exec -T openbao bao policy write api-pii-envelope - < openbao/policies/api-pii-envelope.hcl
```

이후 AppRole 발급과 `openbao_api_credentials` volume 초기화는 operator provisioning 절차가 담당한다. API·migrator는 `/run/openbao/api-role-id`, `/run/openbao/api-secret-id`를 읽고, 해당 volume은 read-only로만 사용한다.

AppRole 초기 provisioning은 HTTPS와 명시적인 CA, owner-only operator token 파일을 요구한다. token 내용은 명령 인자·stdout·로그로 전달하지 않는다.

```bash
OPENBAO_ADDR=https://127.0.0.1:18200 \
OPENBAO_CA_CERT_FILE=data/openbao/tls/ca.crt \
OPENBAO_OPERATOR_TOKEN_FILE=/secure/operator/openbao-root.token \
OPENBAO_CREDENTIALS_VOLUME=infra_openbao_api_credentials \
make openbao-app-role
```

helper는 volume을 먼저 만들고 비어 있는지 확인한다. 기존 파일이 있으면 role이나 SecretID를 재발급하지 않고 실패한다. SecretID는 `secret_id_ttl=0s`로 재기동 동안 유지되며, 수동 rotation은 승인된 maintenance 절차에서 기존 SecretID를 revoke하고 credential volume을 비운 뒤 다시 실행한다. 이 helper는 envelope를 만들지 않으며, API `scripts/provision-pii-envelope.mjs`가 기존 3개 DEK manifest를 준비한 뒤 사용한다. API 전환이 끝나면 기존 `USER_EMAIL_LOOKUP_KEY`·`USER_PII_ENCRYPTION_KEY` raw 환경변수는 유지하지 않는다.

Compose healthcheck는 프로세스와 TLS listener liveness만 확인하므로 초기 sealed 상태도 healthy로 표시한다. API readiness는 별도 startup secret access 검증에서 fail closed 해야 한다.

스냅샷은 권한 있는 token을 숨겨 입력한 뒤 로컬 무시 경로에 저장한다. 기본 경로는 `data/openbao/snapshots/`이며, 경로를 바꾸면 `make openbao-up`와 `make openbao-snapshot`에 같은 `OPENBAO_SNAPSHOT_DIR` 값을 전달한다. Compose bind mount도 이 값을 사용한다. 스냅샷 파일 쓰기는 명시적인 one-shot root exec로 0700 디렉터리 권한을 사용하며, OpenBao 서버 프로세스 자체는 `openbao` UID로 실행된다.

```bash
make openbao-snapshot
```

상태·로그·중지는 다음 명령으로 수행한다. named volume을 지우는 `docker compose down -v`는 사용하지 않는다.

```bash
make openbao-status
make openbao-logs
make openbao-down
```

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
| OpenBao HTTPS | `127.0.0.1:18200` | `8200` |

Compose의 `livekit` 서비스(`livekit/livekit-server:latest`)가 LiveKit dev server를 실행한다.
API와 Voice Agent는
`config/api.env`와 `config/voice-agent.env`의 `LIVEKIT_URL`인 `ws://macbookpro:7880`을
사용한다. Compose에서는 각각 `API_ENV_FILE`과 `VOICE_AGENT_ENV_FILE`로 이 파일을 지정한다.

커스텀 설정은 example 파일을 local 파일로 복사한 뒤 root `.env`의
`API_ENV_FILE`과 `VOICE_AGENT_ENV_FILE`을 복사한 경로로 지정한다.

LiveKit dev server의 기본 개발 credential은 `devkey`/`secret`이며 운영 credential로
사용하지 않는다.

## 로컬 SIP/전화 테스트 스택

Asterisk와 LiveKit SIP는 기본 실행에서 제외된 `telephony` profile로 제공한다.
기존 `livekit`과 `redis`를 재사용하고 Compose 내부 DNS로만 통신하므로, 기본 `make deploy`의
리소스 부담과 기존 named volume에는 영향이 없다.

```bash
make telephony-up
make telephony-provision
make telephony-health
make telephony-logs
```

`make telephony-provision`은 `scripts/sip-provision.sh`를 통해 `sip/`의 로컬 inbound trunk, outbound trunk, dispatch rule을
이름으로 조회한 뒤 없는 리소스만 생성한다. 따라서 두 번 실행해도 중복되지 않으며, 생성 결과에
반환되는 inbound/outbound trunk ID를 기록해 둔다. 실제 전화번호를 연결할 때는 Port Admin/API에서
inbound trunk에 번호와 Asterisk 경로를 바인딩하고, outbound transfer가 필요하면 local voice-agent
환경의 outbound trunk ID를 설정한다. 외부 통신사 trunk, 공인 IP, TLS, 녹음/Egress는 포함하지 않는다.

Echo 테스트 목적의 Asterisk 내선 `600`은 로컬 RTP 범위에서 동작한다. SIP 포트는 loopback에만
바인딩된 `15090`(LiveKit SIP), `15060`(Asterisk), LiveKit SIP health는 `18090`이며 필요하면 `.env`에서
변경할 수 있다. 로컬 inbound 테스트 번호는 `2000`이다. 종료할 때는 `make telephony-down`을 사용한다.

로컬 Compose에서는 adaptor의 PAT identity integration을 비활성화한다. identity endpoint는
HTTPS endpoint를 제공하는 환경에서만 local env로 opt-in하며, `PUBLIC_BASE_URL`은
`http://macbookpro:3002`로 고정한다.

`make health`는 각 컨테이너의 liveness smoke와 LiveKit TCP 포트 검사를 수행한다. Voice Agent의 metrics endpoint는
process liveness만 보장하며 LiveKit registration은 logs와 별도 manual smoke로 확인한다.
Native auth, Web→API, API→RAG 연동도 별도 manual smoke로 확인한다. Aggregator는 distroless
이미지라 컨테이너 healthcheck 대신 host smoke (`3001/healthz`)를 사용한다.

로컬 Compose의 API는 `NODE_ENV=local`을 사용한다. SMTP가 설정되지 않은 로컬에서
회원가입 이메일 인증 코드를 응답으로 반환하고 Web이 자동 입력할 수 있도록 하는 개발 전용 설정이다.
실제 환경에서는 SMTP를 설정하고 `NODE_ENV=production`으로 실행해야 하며, 인증 코드를
응답이나 로그로 노출하면 안 된다. `MAIL_HOST`, `MAIL_USER`, `MAIL_PASS`는 이 로컬 설정에 넣지 않는다.

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
