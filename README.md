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

### macOS Keychain 자동 unseal

Mac 로그인 후 별도 share 입력 없이 기동하려면 Keychain을 신뢰원으로 사용하는 static seal을 설정한다.
외부 KMS 대신 현재 Mac 로그인 Keychain에 32-byte seal key를 보관하는 선택이다. 이 키는 API의 DEK나 Transit KEK와 다르며,
API 컨테이너에는 전달되지 않는다. macOS 로그인 전 FileVault/Keychain 잠금 해제는 이 기능의 범위에 포함되지 않는다.

`make openbao-keychain-prepare`는 Security.framework Swift helper를 사용자 전용
`~/.local/share/port-openbao-autounseal/`에 컴파일·서명한다. `make openbao-keychain-init`은 로그인 세션에서 최초 키를 생성하고,
기존 Keychain 항목이 있으면 유지한다. helper 자체만 trusted app으로 등록하며 키 값을 명령 인자·환경변수·로그에 넣지 않는다.
headless shell에서는 login Keychain 접근이 거부될 수 있으므로 init/check는 일회성 GUI LaunchAgent에서 실행한다.

기존 Shamir 서버 전환 순서:

1. 기존 Raft snapshot 또는 정지 상태의 Raft volume 사본과 unseal shares를 외부 owner-only 보관소에 확보한다.
2. `make openbao-keychain-init`으로 Keychain 키를 준비한다. `.env`의 `OPENBAO_SEAL_MODE`는 아직 `shamir`여야 한다.
3. `.env`에 `OPENBAO_SEAL_MODE=static`을 설정하고 OpenBao만 재생성한다.
4. `make openbao-auto-install`로 로그인 LaunchAgent를 설치한다. controller가 Keychain 키를 읽어 OpenBao 전용 tmpfs
   `/bao/seal-runtime/current.key`에 stdin으로 전달한다. 권한은 `openbao:openbao`, `0400`이다.
5. 최초 전환 때만 `docker compose exec openbao bao operator unseal -migrate`를 서로 다른 기존 shares로 실행해 threshold를 충족한다.
   기존 shares는 이후 recovery keys 역할을 한다. 기존 KEK/DEKs/AppRole/사용자 암호문은 유지된다.
6. `make openbao-status`에서 `type=static`, `sealed=false`를 확인하고 OpenBao 재시작 후 자동으로 동일 상태가 되는지 검증한다.

설치되는 LaunchAgent는 `~/Library/LaunchAgents/com.port.infra.openbao-autounseal.plist`다.
로그인할 때 Colima가 꺼져 있으면 한 번 시작하고 OpenBao의 메모리 키를 준비한다. 이후 수동으로 Colima나 OpenBao를 중지하면
감시 중에 억지로 다시 시작하지 않는다. Colima를 다시 시작하거나 컨테이너가 재시작되면 새 tmpfs에 키를 다시 전달한다.
이미 실행 중인 OpenBao에 운영자가 `seal` 명령을 내린 경우에는 자동으로 취소하지 않는다.

```bash
make openbao-auto-install
launchctl print gui/$(id -u)/com.port.infra.openbao-autounseal
make openbao-status
make openbao-auto-stop
```

로그는 `~/Library/Logs/port-openbao-autounseal/`의 owner-only 파일이며 상태만 기록한다.
Keychain 항목을 읽지 못하면 자동 생성이나 덮어쓰기를 하지 않고 대기한다. helper의 `emit`은 supervisor 내부 pipe 전용이다.
이를 터미널에서 직접 실행하거나 파일로 redirect하지 않는다. helper 재컴파일은 trusted app identity를 바꿀 수 있으므로
installer는 같은 source fingerprint의 바이너리를 재사용하고 변경 시 별도 trust 이전을 요구한다.

Keychain 항목을 삭제하거나 초기화하면 static seal을 복구할 수 없으므로 macOS Keychain 백업과 이전 Shamir 복구 자료를 보존한다.
`make openbao-auto-stop`은 supervisor만 중지하고 Keychain 키를 삭제하지 않는다. 로그아웃 상태에서는 사용자 LaunchAgent가 실행되지 않는다.

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

API의 `WEB_ORIGIN`은 인증 리다이렉트에 사용하는 canonical 웹 주소이며, 로컬 Compose에서는
`http://macbookpro:3000`을 유지한다. `WEB_ALLOWED_ORIGINS`는 canonical origin 외에 CORS와
CSRF 검사에서 허용할 정확한 origin을 쉼표로 구분한 목록이다. 로컬 설정/example은
`http://localhost:3000,http://localhost:3010`을 명시하며, 다른 localhost 포트나 wildcard를
자동 허용하지 않는다. 추가 origin이 필요 없는 환경에서는 이 값을 비워 두거나 생략한다.
허용 목록은 Origin 검사를 비활성화하지 않으며, CSRF 보호 요청에 Origin이 없으면 거부한다.

LiveKit dev server의 기본 개발 credential은 `devkey`/`secret`이며 운영 credential로
사용하지 않는다.

## 음성 미리듣기 저장과 생성 제한

API는 생성된 샘플을 `voice_preview_cache` named volume의 `/app/data/voice-previews`에
저장한다. 컨테이너 재생성 후에도 재사용하며 자동 만료는 없다. 볼륨 사용량을 모니터링하고
백업 대상에 포함한다. 브라우저 응답은 계속 `private, no-store`이며 서버에서만 재사용한다.
컨테이너 없이 실행할 때는 `VOICE_PREVIEW_CACHE_DIR`로 경로를 지정한다(기본 `data/voice-previews`).
개발 Compose는 root로 실행되므로 별도 `voice_preview_cache_dev` 볼륨을 같은 경로에
마운트한다. 일반 runner(UID 1001)의 캐시와 섞지 않아 `0600` 파일의 소유권 충돌을 방지한다.

- 매 요청마다 음성 활성 상태·모델 호환성·커스텀 음성 소유권을 확인한다.
- 카탈로그 샘플은 공유하고 커스텀 샘플은 소유자별로 분리한다.
- 공급자·모델·음성 ID·언어·샘플 문구/형식 버전이 달라지면 새로 생성한다.
- 같은 샘플의 동시 요청은 Redis lease로 합성 한 번만 수행한다. 한 브라우저의 취소는
  다른 요청이나 진행 중인 저장을 취소하지 않는다. 실패한 생성은 음성 파일로 저장하지 않는다.
  서버 간 대기 요청에는 작업 소유자별 실패 원인을 Redis에 60초 보관해 전달한다.
- 새 생성 시도만 60초 고정 창으로 제한한다. 기본 사용자 10회, 전체 60회이며
  `VOICE_PREVIEW_USER_GENERATION_LIMIT`, `VOICE_PREVIEW_GLOBAL_GENERATION_LIMIT`로 조정한다.
  초과 요청은 HTTP 429를 반환한다. 저장된 샘플 재생은 이 한도를 소모하지 않는다.
  다른 사용자의 개인 한도 초과는 전파하지 않는다. 그 경우에만 대기 사용자의 한도로
  생성 진입을 다시 시도하며, 공급자 실패를 자동 재합성하지 않는다.
  요청과 공유 생성 작업은 각각 20초 제한을 적용한다. Redis·파일 I/O 대기도 제한하며,
  락 해제는 별도 1초 제한으로 처리해 이미 저장한 샘플의 응답을 막지 않는다.
  시간 초과 뒤 늦게 획득한 락은 소유자 토큰을 확인해 해제하고 합성을 시작하지 않는다.
- Redis/저장소 장애 때 캐시 미스를 무제한 합성으로 우회하지 않는다. 생성 제한과 lock은
  Redis를 사용하므로 API replica들은 같은 Redis와 **같은 캐시 파일시스템**을 공유해야 한다.
  다른 호스트의 독립 local volume을 같은 공유 저장소로 간주하면 안 된다.

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

### PJSUA CLI 테스트 단말

Mac용 CLI는 `brew install pjproject`로 설치한다. 현재 Colima의 loopback UDP 전달 경로에
의존하지 않도록 반복 통화 테스트는 Asterisk와 같은 `infra_default` Docker 네트워크에서 실행한다.
Docker 이미지는 공식 PJSIP 2.17 소스를 SHA-256 검증 후 빌드한다.

```bash
make pjsua-build
make pjsua-setup
```

`pjsua-setup`은 고객 `1001`, 상담원 `1002`의 임의 비밀번호와 설정을 `asterisk/local/`에
생성한다. 재실행 시 비밀번호를 유지한다. 이 디렉터리의 생성 파일과 `pjsua/artifacts/`는
Git에서 제외된다. 최초 설정은 Asterisk 컨테이너를 재생성하므로 진행 중인 테스트 통화를
종료한 뒤 실행한다. 이후에는 계정 설정을 reload하며, 나머지 서비스와 volume은 유지한다.

두 터미널에서 각각 실행한다.

```bash
# 상담원: 수신 통화 자동 응답
make pjsua-agent

# 고객: CLI에서 아래 call 명령 사용
make pjsua-caller
```

```text
call new sip:600@asterisk     # 에코 테스트
call dump_q                  # RTP 송수신 통계
call hangup
call new sip:601@asterisk     # DTMF 4자리 수신 테스트
call d_2833 1234
call hangup
call new sip:1002@asterisk    # 상담원 내선 통화
call hangup
shutdown
```

위 주석은 설명용이며 실제 CLI에는 `#` 앞의 명령만 입력한다. `601`에서 받은 숫자는
Asterisk 로그의 `PJSUA_DTMF_RESULT=1234`로 확인한다. SIP 인증, G.711 RTP,
RFC 4733 DTMF를 실제 전송한다. 기본 `--null-audio` 모드는 Mac 마이크와 스피커를 사용하지 않는다.
WAV 파일은 `pjsua/artifacts/`에 넣고 컨테이너의 `/artifacts/` 경로로 재생·녹음할 수 있다.

```bash
bash scripts/pjsua.sh caller --play-file=/artifacts/input.wav --auto-play \
  --rec-file=/artifacts/echo.wav --auto-rec sip:600@asterisk
# 거절 / 수동 응답(무응답 시나리오): make pjsua-agent 대신 하나만 실행
bash scripts/pjsua.sh agent --auto-answer=486
bash scripts/pjsua.sh agent
```

`82000`은 기존 LiveKit inbound `2000`으로 연결한다. 실제 AI 통화에는 API의 전화번호·publication
바인딩과 Worker 설정이 별도로 필요하다. LiveKit outbound에서 `1001`/`1002`를 호출하면
해당 등록 단말로 연결하며, 다른 번호의 기존 에코 경로는 유지한다.
단말은 `shutdown`으로 종료하면 컨테이너도 제거된다. 로컬 SIP 테스트는 통신사 PSTN 검증을 포함하지 않는다.

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
