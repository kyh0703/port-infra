# port-infra

로컬 개발에 필요한 상태 저장 서비스와 애플리케이션을 Docker Compose로 실행한다.
애플리케이션은 각 저장소가 GHCR에 발행한 `:dev` 이미지를 사용한다.

## Grafana Cloud 로그 수집 (선택)

`compose.logs.yml`은 독립된 `infra-logs` 프로젝트로 Alloy와 Docker 로그 프록시만
실행한다. 기존 애플리케이션의 Compose 파일 체인·이미지·환경변수는 변경하지 않는다.
기존 `compose.yml`과 병합하거나 `make deploy`로 실행하지 않는다. Sentry 설정도 유지한다.

### 연결 정보 준비

1. Grafana Cloud 스택의 Logs 연결 안내에서 `/loki/api/v1/push` URL과
   Logs tenant ID를 확인한다. 해당 스택의 `logs:write` 권한만 가진 토큰을 생성한다.
2. `config/grafana-logs.env.example`을 `config/grafana-logs.env`로 복사하고
   URL·tenant ID·실제 환경명·토큰 파일의 절대 경로를 설정한다.
3. 토큰은 Git 밖의 파일에 **토큰 문자열만, 끝 줄바꿈 없이** 저장하고 권한을
   `chmod 600 /absolute/path/to/token`으로 제한한다. 토큰은 env 파일에 넣지 않는다.
   macOS/Colima에서는 Docker VM에 공유되는 홈 디렉터리 아래 경로를 사용한다.
   `/tmp`는 VM에서 같은 경로로 보이지 않을 수 있다.

`ALLOY_TARGET_PROJECT`의 기본값은 `infra`다. `ALLOY_ENVIRONMENT`는 필수이며
로컬에서는 `local`, 운영에서는 `production` 등 실제 환경에 맞게 설정한다.
`ALLOY_DOCKER_SOCKET`은 Docker daemon 호스트 안의 socket 경로다.
Colima의 macOS CLI socket 경로가 아니라 기본 `/var/run/docker.sock`을 사용한다.

### 실행·검증·중지

infra 저장소 또는 해당 작업 worktree에서 실행한다.

```bash
cp config/grafana-logs.env.example config/grafana-logs.env
# 위 파일의 연결 정보를 수정하고 토큰 파일을 준비한 뒤:
docker compose --env-file config/grafana-logs.env -f compose.logs.yml config --quiet
docker compose --env-file config/grafana-logs.env -f compose.logs.yml run --rm --no-deps \
  alloy validate /etc/alloy/config.alloy
docker compose --env-file config/grafana-logs.env -f compose.logs.yml up -d
docker compose --env-file config/grafana-logs.env -f compose.logs.yml logs --tail=100 alloy docker-log-proxy
```

Alloy 설정 문법 검증만으로 인증·파일 접근·실제 전송이 검증되지는 않는다.
API 요청과 테스트 통화를 발생시킨 뒤 Cloud Explore의 Logs 데이터소스에서 확인한다.

```logql
{service_name=~"api|voice-agent", environment="local"}
```

```logql
{service_name=~"api|voice-agent", environment="local"}
  | json
  | conversationId="실제-통화-ID"
```

동일 ID가 기록된 로그만 검색된다. `conversationId`·`executionId`·`requestId`는
JSON 본문에 유지하며 인덱스 라벨로 만들지 않는다. 환경명이 다르면 쿼리도 바꾼다.

```bash
# 수집 중지. 앱과 수집 위치 볼륨은 유지한다.
docker compose --env-file config/grafana-logs.env -f compose.logs.yml stop
# 다시 시작
docker compose --env-file config/grafana-logs.env -f compose.logs.yml up -d
```

`alloy_data`는 읽은 위치를 보존한다. 운영에서 `down -v`로 지우지 않는다.
전송 장애·Docker 로그 회전 중 무손실 보관을 보장하는 큐는 아니다.
Alloy 로그의 전송 실패와 Cloud 사용량을 함께 확인한다.

### 수집 범위와 보안

- 지정 프로젝트의 `api`·`voice-agent`만 수집한다. 일회성 `compose run` 컨테이너,
  Web·DB·Alloy 로그는 제외한다. 호스트가 여러 대면 호스트별 수집기가 필요하다.
- 최상위 Pino `level`이 `10`·`20` 또는 문자열 `trace`·`debug`인 로그는 버린다.
  info 이상과 JSON이 아닌 시작 오류는 보존한다. 기존 Docker 로그 이력도 최초 실행 때
  읽힐 수 있으므로 활성화 전에 과거 로그의 민감정보도 확인한다.
- **본문은 원본 그대로 전송한다. 범용 개인정보 마스킹이나 health 요청 제거는 하지 않는다.**
  토큰·Authorization·Cookie·전화번호·대화 원문·도구 인자/응답이 원본에 기록되면
  Cloud에도 전송된다. 운영 활성화 전 앱 로거의 redaction을 확인한다.
- Cloud 토큰은 Compose file secret으로 Alloy에만 마운트한다. 환경변수에 토큰을 넣지 않는다.
  로컬 Compose secrets는 암호화된 secret 저장소가 아니라 파일 마운트다.
- Docker socket을 가진 프록시는 강한 권한을 가진다. `:ro`는 Docker API 권한을 제한하지 않는다.
  프록시는 읽기용 discovery·inspect·logs 경로만 허용하고 변경 요청·exec·archive를 차단한다.
  **inspect는 컨테이너 환경변수를 읽을 수 있다.** 수집 대상 필터는 Alloy 설정이지
  프록시의 컨테이너별 인가가 아니다. 내부 네트워크를 신뢰 경계로 유지한다.
- 두 서비스 모두 호스트 포트를 공개하지 않는다. 프록시는 내부 네트워크에만,
  Alloy만 별도의 외부 전송 네트워크에 연결한다. Alloy는 read-only root filesystem,
  `no-new-privileges`, 파일 접근용 `DAC_OVERRIDE` 외 capability 제거를 적용한다.
- 메모리 제한은 Alloy 256MiB, 프록시 64MiB이며 이미지 digest를 고정한다.
  업그레이드할 때 태그와 digest를 함께 갱신하고 실제 수집을 재검증한다.

자체 Loki로 전환할 때는 Alloy 수집 구조를 유지하고 목적지·인증을 조정한다.
Cloud의 과거 로그는 자동 이전되지 않는다.

검증: 격리된 Docker fixture와 실제 Loki로 API·Voice-agent 수집, 다른 프로젝트·Web·
일회성 컨테이너 제외, debug 제외, 중첩 payload의 level 보존, 일반 문자열 오류 수집을 확인했다.
Alloy 재시작 후 새 로그 수집과 프록시의 POST·archive·export 차단도 확인했다.
새 인프라 설정은 사전 단위 테스트 대신 실제 컨테이너 smoke로 검증했다.
2026-09-29 로컬 수집기에서 Grafana Cloud 인증·전송을 확인했다:
HTTP 204 응답 17회, 전송 19,509건, 전송 실패 drop 0건(확인 시점 누적).
이는 전송 확인이며 Cloud 화면에서 모든 로그를 직접 조회했다는 의미는 아니다.
토큰과 연결 env 파일은 Git에서 제외한다.

Docker 로그 읽기는 제한된 프록시에 `tcp://`로 연결한다. Alloy 1.14의 `http://`
경로는 `refresh_interval`을 HTTP 전체 요청 제한 시간으로 사용해 긴 로그 스트림을
주기적으로 끊는다. 같은 내부 프록시에 Docker TCP transport로 연결하면 이 제한을
피하면서 프록시의 API 허용 목록은 유지된다. 35초 간격 로그 두 건을 실제 Loki에서
조회하고 해당 구간의 스트림 timeout 오류가 없음을 확인했다.

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

전체 플랫폼을 시작하기 전 API와 Worker의 비공개 환경파일을 준비한다. 기존 파일은 덮어쓰지 않는다.

```bash
cp -n config/api.env.example config/api.local.env
cp -n config/voice-agent.env.example config/voice-agent.local.env
chmod 600 config/api.local.env config/voice-agent.local.env
```

두 파일의 `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`에 동일한 LiveKit Cloud 프로젝트의
접속 주소와 자격증명을 설정한다. 키와 secret은 서버 전용이며 example 파일이나 Git에 넣지 않는다.
복사한 root `.env`의 example 선택값도 다음 비공개 경로로 바꾼다.

```dotenv
API_ENV_FILE=./config/api.local.env
VOICE_AGENT_ENV_FILE=./config/voice-agent.local.env
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

현재 로컬 Cloud 전환 릴리스는 비공개 `config/livekit-cloud.local.yaml` 이미지 overlay를 사용한다.
실행 중인 stack을 변경할 때는 기존 Compose `-f` 목록을 유지하고 이 파일을 마지막에 적용한다.
기본 이미지 운영용인 위 `make deploy`·`make recreate`를 그대로 실행해 현재 릴리스를 교체하지 않는다.

#### Spaces Supervisor 릴리스 (2026-10-02)

초기 feature image pin은 `config/spaces-supervisor-main.local.example.yaml`에 있다.
이는 이미지 전용 overlay다. 현재 서비스의 전체 Compose 파일 목록·프로젝트명 `infra`·환경변수·
mount·port·network를 유지한다. 그 뒤 추가된 trusted-ingress 설정도 되돌리지 않는다.
`make deploy`로 최신 main 이미지를 무작정 적용하지 않는다. Web main에 합쳐진 월 구독 변경은
이번 Spaces 배포 범위가 아니며, 기존 billing API와 혼합 배포하면 안 된다.

현재 Web은 `config/spaces-supervisor-history.local.example.yaml`의 local immutable image
`sha256:aff59c4f7d9ad1d4880706a2d1a64715db1e23bf1c310bf8dc40b2e09efa43f2`를 사용한다.
release-compatible source `9f9f88a7`에 history fix `e45dd0ad`와 기존 실행 중인 ingress patch
`38871827`만 결합했다. 이 image 전용 overlay는 **기존 전체 파일 목록의 마지막**, 특히
`trusted-ingress.local.yaml` 뒤에 둔다. 파일만 복사해 다른 호스트에 배포할 수는 없다.
먼저 보호된 image archive를 `docker image load --input`으로 가져오거나 동일 source patch로 빌드해야 한다.
파일 예제의 `pull_policy: never`는 다른 image로 조용히 바뀌는 것을 막는다.

접속 주소는 `https://macbookpro.tail9f349d.ts.net:8443/spaces`다. Tailscale 접속이 필요하다.
기존 `macbookpro:3000`·직접 API 포트는 재개방하지 않는다. Compose 설정을 재평가할 때는
실행 중인 ingress의 `INGRESS_PUBLIC_AUTHORITY`, `INGRESS_TRUSTED_EDGE_CIDR`,
`INGRESS_PORT`도 유지한다. 최종 history fix는 `up --no-deps --pull never --wait web`으로
Web만 교체했으며 API·worker·Caddy 설정과 다른 서비스/볼륨을 변경하지 않았다.

DB cutover 전에 custom-format dump와 roles를 보호된
`backups/db/spaces-supervisor-main-20261002T093415Z/`에 저장했다.
별도 PostgreSQL에 실제 전체 restore 후 Spec `4dc0cf0f`의 두 UP migration,
`node scripts/upgrade-spaces-v2.mjs` dry-run, `--apply`, 반복 실행을 검증했다.
초기 운영 ledger는 157→159가 됐다. v1 Space 1건을 보호된 Supervisor root를 가진 numeric v2로
한 번만 변환했다. API GET-time 변환은 없다. 동시 작업으로 나중에 추가된 migration을 내리지 않는다.
실제 서비스의 quiescence와 Cloud room/participant drain 뒤 초기 유지보수는 약 80초였다.
원본 Agent 11건·발행본 36건은 복원본과 모든 원본 필드를 대조해 보존을 확인했다.
백업 복원은 새 데이터 유실을 일으킬 수 있으므로 자동 rollback이나 SQL DOWN을 실행하지 않는다.

최종 HTTPS에서 첫 메시지 응답, immutable solo publication, 실제 전문가 `agent_task` 완료와
Supervisor 복귀, drag/content 독립성, 모바일 삭제·포커스와 실제 Back/Forward/±2 취소를 확인했다.
Chrome history 50개 한도에서도 native fragment/router state와 초안이 유지됐다.
검증 Space·통화 이력은 정식 API로 삭제하고 로그아웃했다. 테스트 계정 탈퇴는 잔여 interaction
session 4건 때문에 `409 WITHDRAWAL_ACTIVE_WORK`로 거절됐다. 실제 Cloud room/participant,
진행 중 통화·audit·usage는 0이다. 계정과 signup 기본 데이터는 보존했고 DB fence를 우회하거나
가짜 webhook을 보내지 않았다. 상세 증거·image archive·재현 patch는 보호된 배포 백업에 있다.

### Jev 음성사서함 감지 (선택)

OpenRouter 키를 등록하는 것만으로 감지가 활성화되지는 않는다. API와 worker 양쪽의
`JEV_VOICEMAIL_ENABLED=true`, 동일한 `JEV_ALLOWED_PUBLISHED_IDS`, 기존 내부 인증 키가 필요하다.
키는 API의 기존 암호화된 provider credential 저장소에서 읽으며 Compose에 새로 넣지 않는다.

`config/jev-voicemail.local.example.yaml`은 이미지·볼륨을 바꾸지 않는 환경변수 전용 overlay다.
API 제한 시간은 400ms, worker 제한 시간은 600ms이며 `JEV_ENDPOINTING_ENABLED=false`를 유지한다.
예제는 `JEV_ALLOWED_PUBLISHED_IDS` 환경변수를 필수로 받는다. 배포용
`config/jev-voicemail.local.yaml`에 실제 ID 목록을 문자열로 고정하면 재기동 시에도 범위가 유지된다.
이 로컬 파일은 Git에서 제외한다. 허용 목록은 실제 활성 publishedId를 쉼표로 구분하며
전체 허용 wildcard를 사용하지 않는다.

기존 `.env`가 준비된 infra 루트에서 로컬 overlay를 준비한 뒤 실행한다.
현재 서비스별 Compose 파일 체인을 유지하고 이 overlay를 마지막에 적용한다.
API와 worker의 기존 릴리스 체인이 다를 수 있으므로 한 서비스의 체인을 다른 서비스에 재사용하지 않는다.
기존 통화가 없는 시점에 API, worker 순서로 갱신한다.

```bash
JEV_OVERLAY="${PWD}/config/jev-voicemail.local.yaml"
for service in api voice-agent; do
  files="$(docker inspect "infra-${service}-1" \
    --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')"
  case "$files" in
    "$JEV_OVERLAY"|*,"$JEV_OVERLAY") ;;
    *) files="${files},${JEV_OVERLAY}" ;;
  esac
  COMPOSE_PATH_SEPARATOR=, COMPOSE_FILE="$files" \
    docker compose --project-directory "$PWD" --env-file .env -p infra \
    up -d --no-deps --pull never --wait --wait-timeout 90 "$service" || break
done
```

다음 배포에도 이 overlay를 마지막에 유지한다. 새 publication을 발행하면 ID가 바뀌므로
허용 목록을 갱신하고 양쪽 서비스를 다시 적용한다. 종료된 통화·텍스트 통화·비-SIP 세션은
음성사서함 판단 대상이 아니다. 판단 실패·시간 초과 시 통화는 유지하며,
유효한 `voicemail` 분류가 반환되면 추가 점수 문턱 없이 종료를 요청한다.
종료 API 승인과 제어권 재확인 후 LiveKit room을 삭제한다.

2026-09-30 로컬 검증:

- 활성 publication 12개를 양쪽 허용 목록에 적용하고 기존 API·worker 이미지를 유지했다.
  API·worker·Redis·PostgreSQL health와 worker의 LiveKit 등록을 확인했다.
- 실제 worker → API 요청에서 없는 세션은 404, 비활성 EOT는 403으로 거절됐다.
- 기존 암호화된 OpenRouter 키로 `typesafe/jev-1.13-20260917`의 실제 HTTP 200 응답을 받았다.
  400ms 요청 제한 안에서 영어 사서함 안내는 214ms, 확률·신뢰도 0.98로 판정됐다.
  한국어 인사말은 286ms에 `human`으로 판정됐다.
- 최초 한국어 사서함 샘플은 확률·신뢰도 0.95로 분류됐지만 당시 이중 0.98 정책에 막혔다.
  이후 아래 수정에서 이 점수 문턱을 제거했다.
  첫 공급자 요청은 약 479ms였으므로 400ms 제한을 넘길 수 있다.
  이 수치는 지연 보장이나 한국어 사서함 감지율 검증이 아니다. 실제 SIP 통화 종료는 검증하지 않았다.
- 활성화 전 Redis AOF 손상으로 API가 재시작 중이었다. 원본 전체를
  `data/backups/redis-before-jev-20260930T023142Z/redis-data.tar`에 백업한 뒤,
  승인된 `redis-check-aof --fix`로 손상된 incremental AOF 꼬리 3,789,800바이트를 제거했다.
  원본 백업은 보관하며 PostgreSQL 데이터와 기존 키는 변경하지 않았다.
- 사서함 자동 종료 수정은 worker 이미지
  `port-voice-agent:jev-voicemail-termination-20260930`으로 배포했다.
  `config/jev-voicemail-termination.local.yaml`은 기존 worker 체인 마지막에 붙이는 이미지 전용 override다.
  API 이미지·기존 환경변수·허용 publication 12개·EOT OFF는 변경하지 않았다.
  실제 Jev 응답을 HTTP 경계에서 재생한 배포 worker smoke에서 한국어·영어 사서함의
  종료 콜백, 실제 LiveKit room 삭제와 RTC 연결 해제를 확인했다. 사람 응답은 연결을 유지했다.
  외부 SIP 전화 전체 경로는 검증하지 않았으며 검증용 room과 임시 컨테이너는 제거했다.

2026-09-30 main 병합 후 최종 로컬 배포:

- worker main 병합 커밋은 `be4e7db3ab0cb960c69d6003d300eac723449ba7`,
  infra 활성 설정 커밋은 `f9def8581dc2d026289ab2f86da91e8f3ff9adff`다. 두 저장소 모두 main에 push했다.
- [main의 build-dev 실행](https://github.com/kyh0703/port-voice-agent/actions/runs/36671724193)은
  GitHub 계정 결제/지출 한도 문제로 runner가 시작되지 못했다. 빌드 단계가 실행되지 않았으며
  이 workflow에서는 GHCR 이미지를 발행하지 못했다. 결제/한도 문제 해결 전 자동 발행은 차단 상태다.
- 대신 위 worker 커밋의 `git archive`로 추적된 파일만 빌드했다. Node 20·linux/arm64 로컬 이미지
  `port-voice-agent:main-be4e7db3ab0c`의 OCI revision은 병합 커밋 전체 SHA와 일치한다.
  `config/jev-voicemail-termination.local.yaml`을 이 이미지로 갱신하고 worker만 재생성했다.
  Compose의 다른 설정·볼륨·환경변수는 같으며 API 컨테이너는 재생성하지 않았다.
- 병합된 main에서 테스트 888개, typecheck, build가 통과했다. 배포 후 API·worker·Redis·PostgreSQL은
  healthy이고 worker는 LiveKit에 등록됐다. 양쪽 허용 목록은 DB의 활성 publication 12개와 정확히 같고,
  음성사서함 ON·EOT OFF다. 실제 worker → API 요청의 없는 세션 404·비활성 EOT 403도 확인했다.
- 기존 암호화된 키와 운영 분류 프롬프트로 얻은 새 Jev HTTP 200 응답을 배포 이미지의 HTTP 경계에서
  재생했다. 한국어 사서함은 확률 0.77·신뢰도 0.71에서도 종료 콜백 1회, 실제 LiveKit room 삭제,
  RTC 연결 해제를 확인했다. 영어 사람 응답은 확률 0.97·신뢰도 0.96의 `human`으로 연결을 유지했다.
  검증용 room은 모두 제거했다. 외부 SIP 전화와 실제 STT를 포함한 전체 경로 검증은 아니다.
- 이번 모델 warm-up은 693ms였고 이후 일부 요청도 기존 400ms 제한을 초과했다.
  제한 시간은 늘리지 않았다. 시간 초과·판정 실패 시 통화를 유지하는 기존 fail-open 정책이 남는다.
  모델 응답 시간이나 한국어·영어 감지 정확도를 보장하는 검증은 아니다.

### Ghost 블로그 (선택)

`blog` 프로필은 Ghost 6와 전용 MySQL 8.0을 실행한다. 포털 소스나 기존 PostgreSQL을
사용하지 않으며, 기본 `make up`에는 포함되지 않는다. 기존 블로그 서버·DNS·콘텐츠는 변경하지 않는다.
[Ghost 공식 Docker 안내](https://docs.ghost.org/install/docker)를 참고한다.

기존 `.env` 초기화가 끝난 infra 루트에서:

```bash
docker compose -p infra --profile blog up -d --wait ghost
docker compose -p infra --profile blog logs --tail=100 ghost ghost-db
docker compose -p infra --profile blog stop ghost ghost-db
```

- 블로그: `http://localhost:2368`, 관리자 최초 설정: `http://localhost:2368/ghost/`.
- `GHOST_PORT`와 `GHOST_URL`은 `.env.example` 참고. 포트를 바꾸면 URL의 포트도 함께 변경한다.
- 기본 바인딩은 `127.0.0.1`이다. `GHOST_BIND_HOST=0.0.0.0`이면 MacBook의 LAN·Tailscale
  인터페이스로 접근할 수 있다. MySQL은 전용 내부 네트워크에만 연결하며 포트를 공개하지 않는다.
- 글·계정은 `ghost_db_data`, 업로드 이미지는 `ghost_content` named volume에 보존한다.
  Tello 테마·카테고리 라우팅은 아래의 저장소 파일을 읽기 전용으로 연결한다.
  중지·재생성 때 같은 project name을 유지하고 `down -v`는 사용하지 않는다.
- DB 비밀번호는 최초 초기화 시 반영된다. 기존 볼륨이 있으면 환경변수만 바꿔도 DB 비밀번호가 바뀌지는 않는다.

기존 플랫폼을 건드리지 않고 `http://macbookpro:2368`로 보는 별도 미리보기:

```bash
INTERNAL_SERVER_KEY=ghost-compose-validation-only \
GHOST_BIND_HOST=0.0.0.0 GHOST_URL=http://macbookpro:2368 \
  docker compose --env-file .env.example -p infra-ghost-preview \
  --profile blog up -d --wait ghost
```

이 명령의 키는 전체 Compose 파싱에만 필요하며 Ghost로 전달되지 않는다.
미리보기 종료 시 같은 명령의 `up -d --wait ghost`를 `stop ghost ghost-db`로 바꾼다.
`infra`와 `infra-ghost-preview`는 별도 볼륨을 사용하며 동일한 호스트 포트로 동시에 실행할 수 없다.

`macbookpro`는 이 Mac의 Tailscale 이름이다. 접속 기기는 같은 Tailnet에 연결되어 있어야 한다.
이 바인딩은 Tailnet 전용 제한이 아니라 LAN에서도 접근 가능한 설정이다.
관리자 최초 설정 전에는 접속 가능한 다른 사용자가 소유자 계정을 만들 수 있으므로
신뢰하는 네트워크에서 `/ghost/`의 최초 설정을 완료한다. 공용 인터넷에는 이 HTTP 포트를 노출하지 않는다.
재기동 시 위 두 `GHOST_*` 값을 유지하거나 로컬 `.env`에 설정해야 URL·바인딩이 되돌아가지 않는다.

이 구성은 로컬 블로그 확인용이다. 공개 운영 전에는 DB 비밀번호 교체(최초 기동 전),
HTTPS 리버스 프록시, 실제 도메인에 맞는 `GHOST_URL`, SMTP 설정과 DB·content 백업이 필요하다.
프록시는 원래 HTTPS 요청임을 `X-Forwarded-Proto`로 전달해야 한다.
기존 `blog.telloai.io` 데이터 이전, 뉴스레터 발송 설정, 별도 Analytics·ActivityPub 서비스는 포함하지 않는다.
이미지는 `ghost:6-alpine`과 `mysql:8.0` 계열 태그를 사용하므로 업데이트 전 백업 후 검증한다.

#### overthinker 테마 소스 관리

`tello-ai/infra`의 `ghost/theme`을 커밋
`edd03ccea24fd2d06239f2cc3789c4e4672f4e82` 기준으로 이관했다.
이제 이 저장소의 `ghost/theme/tello/`와 `ghost/theme/routes.yaml`이 원본이다.
office 저장소와 자동 동기화하지 않으며 Git push만으로 운영 서버에 배포되지는 않는다.
공개 브랜드는 `overthinker`로 변경했다. 기존 활성화 설정과 볼륨 경로를 유지하기 위해
내부 테마 ID·디렉터리 이름만 `tello`로 유지한다. 헤더는 SVG 궤도 심볼·텍스트 워드마크를 쓴다.
UI 글꼴은 Pretendard Variable v1.3.9를 jsDelivr에서 동적 서브셋으로 로드한다.
로고·제목·본문·메타데이터에 적용하며 코드 블록만 시스템 고정폭 글꼴을 사용한다.
히어로 조형물 아래의 장식용 FIG. 캡션은 표시하지 않는다.

처음 설치하면 `/ghost/`에서 관리자 계정을 만들고 Settings → Design & branding의
테마 선택 화면에서 `tello`를 활성화한다. 테마 파일 마운트와 활성화는 별개이며,
활성화 선택은 MySQL에 저장된다. 기존 설정을 자동으로 덮어쓰지 않는다.
현재 `infra-ghost-preview`에는 미리보기용으로 `tello`를 활성화했다.

- `ghost/theme/tello/`: Handlebars 템플릿·로고·JS·컴파일된 CSS와 Tailwind 원본.
- `ghost/theme/routes.yaml`: `/insight/`, `/tech/`, `/release/` 채널.
  글에 `insight`, `tech`, `release` 태그를 붙이면 해당 목록에 노출된다.
- 템플릿·라우팅 변경 후 `docker compose -p infra --profile blog restart ghost`.
  미리보기에서는 앞서 설명한 env 옵션과 project name을 사용한다.
- CSS·히어로 원본 수정 시 테마 디렉터리에서 `npm ci`와 `npm run build`를 실행하고
  `assets/css/screen.css`와 `assets/js/hero.js`를 함께 반영한다. 초기 실행에는 빌드가 필요 없다.
  Three.js는 고정 버전으로 로컬 번들에 포함하므로 실행 중 CDN에서 가져오지 않는다.
- `src/hero.js`는 Three.js 매듭·궤도 조형물이다. 다크/라이트 팔레트, 포인터 반응,
  최대 30fps·DPR 1.5 제한을 적용한다. 화면 밖·비활성 탭에서는 중지하고,
  모션 줄이기에서는 정지 화면을 표시한다. WebGL 미지원 시 CSS 궤도 윤곽을 유지한다.
- 읽기 전용 마운트이므로 관리자에서 테마·라우팅을 덮어쓰지 말고 저장소에서 수정한다.
  `ghost-content-init`은 content 볼륨 소유권만 초기화하고, Ghost는 `node` 사용자로 실행한다.
  테마 원본에는 소유권 변경을 하지 않는다.

헤더 우측의 **문의하기**는 `https://macbookpro.tail9f349d.ts.net:8443/contact`로 이동한다.
현재 주소는 같은 Tailnet에 참여한 기기에서 사용할 수 있다.
RSS 링크는 유지하며 640px 이하에서는 헤더 RSS만 숨겨 문의 버튼 공간을 확보한다.
본문을 64px 넘게 스크롤하면 기존 액션 바가 상단 중앙으로 이동한다.
이 상태에서는 바에 20px 블러·채도 160%, 얇은 테두리와 안쪽 하이라이트를 적용하고
**문의하기**를 테마별 반투명 버튼으로 전환한다. 상단 원래 버튼과 RSS 색상은 유지한다.
블러는 바 한 겹에만 적용하며, 블러 미지원·투명도 감소 설정에서는 불투명 배경을 사용한다.
CSS만 변경할 때는 테마 디렉터리에서 `npm run build:css`로 `assets/css/screen.css`를 갱신한다.
공개 도메인이 준비되면 `ghost/theme/tello/default.hbs`의 문의 주소를 변경한다.
미리보기의 사이트 제목도 `overthinker`로 변경했다. 새 설치에서는 관리자 General에서
사이트 제목을 설정한다. 운영 글·계정·언어·코드 삽입 설정·업로드 이미지는 가져오지 않았다.
운영 HTML의 Ghost 버전은 6.55, 로컬 검증 버전은 6.65이며 운영 DB는 변경하지 않았다.

검증: `macbookpro:2368`에서 실제 WebGL 렌더·다크/라이트·320/390/1440px 화면 확인.
모션 줄이기·화면 밖 상태에서는 WebGL draw 호출이 0이고 다시 노출하면 재개됨을 확인했다.

### RAG 관리자 임베딩 키

- RAG는 관리자 `/admin/keys`에 등록한 OpenAI 키를 항상 사용한다. 임베더 선택 환경변수는 없다.
- RAG의 `API_INTERNAL_BASE_URL`은 `http://api:8000/api/v1`이며, 기존
  `INTERNAL_SERVER_KEY`로 인증해 현재 관리자 키를 조회한다. 별도 OpenAI 키 환경변수는 사용하지 않는다.
- API부터 갱신한 뒤 RAG를 갱신한다. 키 누락·조회 실패는 오류로 처리하며 fake로 우회하지 않는다.
- 관리자 키 교체는 다음 임베딩 요청부터 반영된다. `fake`로 색인한 기존 문서는
  실제 임베딩으로 재색인해야 한다. 가짜 임베더는 단위 테스트에만 남기며 운영 이미지에는 포함하지 않는다.

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

`dev-stop`은 `ingress`·`api`·`web`만 중지하며 PostgreSQL·Redis 등 named volume은 삭제하지 않는다.
`make dev-up` 실행 전에는 기본 Compose로 PostgreSQL, Redis와 같은 infra 의존 서비스가 이미
실행 중이어야 하며, 이 명령은 API·Web과 단일 ingress만 build/recreate한다.
개발 모드는 primary infra 저장소 루트에서 실행해야 하며,
API는 `NODE_ENV=development`, Web은 `pnpm dev`로 실행된다. 기본 이미지 기반 실행으로
돌아가려면 `make up`을 사용한다.

## Web/API trusted ingress

Tailnet HTTPS `:8443`은 host-level Tailscale Serve에서 loopback Caddy `:8088`로 연결한다.
정확한 `/api/v1`과 `/api/v1/*`는 API `:8000`, 다른 경로는 Web `:3000`으로 직접 분기한다.
Web의 Next API rewrite와 Web용 `API_INTERNAL_BASE_URL`은 제거했다. RAG의 내부 API 조회는 유지한다.
Funnel이나 서비스별 Tailscale 컨테이너는 사용하지 않으며 인터넷 공개 ingress는 제공하지 않는다.

root `.env`의 `INGRESS_PUBLIC_AUTHORITY`는 scheme/path 없는 canonical HTTPS `host:port`다.
`INGRESS_PORT` 기본값은 `8088`이며 host publication은 `127.0.0.1`로 고정한다.
API 환경파일의 `WEB_ORIGIN`·`API_PUBLIC_BASE_URL`·`GOOGLE_OAUTH_WEB_ORIGIN`은
`https://${INGRESS_PUBLIC_AUTHORITY}`와 같아야 한다. `AUTH_SESSION_COOKIE_SECURE=true`로
두 인증 cookie의 `Secure`·`__Host-` 기본값을 사용하고, 기존 직접 HTTP origin은 허용하지 않는다.

전용 bridge `172.30.40.0/24`의 gateway는 `.1`, Caddy는 `.2`, API는 `.3`, Web은 `.4`다.
Caddy는 실제 Mac→Colima peer인 `172.30.40.1/32`만 신뢰하고 XFF를 오른쪽부터 판독한다.
API는 Caddy `172.30.40.2/32`만 신뢰한다. Caddy는 하나의 client IP로 XFF를 덮어쓰고
`Forwarded`·`X-Real-IP`를 제거하며 Host/forwarded host/proto를 canonical HTTPS 값으로 설정한다.
Web/API HTTP 포트는 호스트에 공개하지 않는다. gRPC `:8080`도 loopback에서만 사용할 수 있다.
호스트와 Docker 관리권한은 신뢰 경계다. 비신뢰 프로세스와 공유하는 호스트에는 이 구성을 적용하지 않는다.
다른 Docker 런타임에서 peer가 달라지면 먼저 실제 주소를 관측한다. private range나 hop 수로 완화하지 않는다.

```bash
make tailscale-ingress
tailscale serve status --json
curl -fsS https://macbookpro.tail9f349d.ts.net:8443/api/v1/health
```

활성화 명령은 HTTPS `8443`만 설정한다. global Serve/Funnel reset을 실행하지 않으며,
별도 `443` handler는 유지한다. 중지는 `tailscale serve --https=8443 off`만 사용한다.

운영 Compose base와 Caddyfile은 primary infra의 `compose.yml`·`ingress/Caddyfile`을 사용한다.
API/Web 각각의 기존 Compose 파일 체인은 유지하고 비공개 `config/trusted-ingress.local.yaml`을
적용한다. 기존 root `.env`와 `config/trusted-ingress.env`를 모두 `--env-file`로 전달해야 한다.
배포 설정·환경파일·bind mount에 feature worktree 경로를 넣지 않는다.

현재 릴리스의 이미지 선택값은 서비스별 마지막 overlay에 유지한다. 기본 `make deploy`/
`recreate`로 기존 release overlay나 검증한 이미지를 덮어쓰지 않는다.
main push와 GHCR 이미지 발행은 별도 작업이다. 이미지 변경은 main의 수동 `build-dev`
발행 결과를 확인한 뒤 적용하며, 경로 정리만을 위해 실행 중인 이미지를 교체하지 않는다.

검증: 실제 Caddy 회귀 9건에서 namespace·요청 본문/인증 헤더·strict IP·대체 헤더 제거·
redirect/복수 cookie·SSE flush·양방향 WebSocket·credential redaction을 확인했다.
실제 HTTPS API health와 PC/모바일 로그인 화면, 잘못된 로그인 `401`, 과거 HTTP origin `403`,
canonical Google callback과 `Secure` OAuth state cookie를 확인했다. Google 계정 로그인 완료는 검증하지 않았다.
trusted edge의 두 client IP는 별도 Redis 예산을 사용했고 HTTPS 위조 헤더는 실제 Tailnet IP 예산에만 반영됐다.
비신뢰 Web peer의 직접 API/Caddy 요청도 전달 헤더를 신뢰하지 않았다.
host/Tailnet의 직접 Web/API HTTP 접속과 Tailnet→ingress `8088` 접속은 거부됐다.
별도 Serve `443`과 나머지 컨테이너 8개의 ID, 기존 credential/cache volume과 API 환경파일은 유지했다.
Ghost profile은 실행하지 않았다. 문의 링크의 새 HTTPS 목적지는 실제 `200`으로 확인했다.

2026-10-03 main 병합 후 경로 전환:

- API/Web/ingress의 Compose base·working directory·환경파일과 Caddy bind mount는
  primary infra 경로다. 실행 컨테이너의 배포 경로에 `.worktrees/` 참조가 없음을 확인했다.
  서비스별 release overlay 체인·기존 이미지·볼륨을 유지한 채 세 서비스만 재생성했다.
- HTTPS login/health는 `200`, `/api/v1`은 API JSON `404`, 인접 `/api/v10`은 Web HTML `404`다.
  잘못된 cookie 로그인은 `401`, 과거 HTTP/위조 origin은 `403`이고 인증 쿠키는 발급하지 않았다.
  두 trusted-edge IP는 별도 예산을 썼고, 위조 XFF·대체 IP 헤더는 실제 peer 예산에만 반영됐다.
  직접 Web/API 및 Tailnet ingress HTTP 포트는 거부됐으며 별도 Serve와 다른 8개 컨테이너는 유지됐다.
- API 최초 기동의 wait는 PostgreSQL 인증 오류 `28P01`로 실패했다. 같은 설정의 3회 재시작 뒤
  healthy와 실제 HTTP 응답을 확인했다. 비밀번호 수정이나 데이터 삭제는 수행하지 않았다.
- 이후 병행 릴리스가 API `7ccf6246`·Web `0a94a29b` main 이미지를 발행·적용했다.
  최신 release overlay를 덮어쓰지 않았으며 두 서비스는 healthy이고 primary 경로를 유지한다.

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
| Web | 없음 | `3000` |
| Adaptor | `3002` | `3000` |
| API HTTP | 없음 | `8000` |
| API gRPC | `127.0.0.1:8080` | `8080` |
| Trusted ingress | `127.0.0.1:8088` | `8088` |
| RAG HTTP | `8001` | `8000` |
| Voice Agent metrics | `19091` | `9091` |
| Aggregator | `3001` | `3000` |
| OpenBao HTTPS | `127.0.0.1:18200` | `8200` |

미디어 전송은 LiveKit Cloud를 사용하며 로컬 LiveKit 서버는 실행하지 않는다.
API는 `LIVEKIT_URL`을 브라우저에 반환하고, Worker는 자신의 환경파일에서 같은 Cloud URL을 읽는다.
접속 주소 형식은 `wss://YOUR_PROJECT.livekit.cloud`다. 로컬 미디어 포트나 Docker 호스트의
ICE 주소를 광고하는 설정은 필요하지 않다.

Compose는 기본적으로 `config/api.local.env`와 `config/voice-agent.local.env`를 읽는다.
API와 api-migrator는 같은 `API_ENV_FILE` 선택값을 사용한다. `.env.example`은 config 검증용으로
example 파일을 명시하므로, 실제 실행에서는 위 비공개 경로를 선택해야 한다.
API와 Worker의 Cloud URL·API key·secret은 같은 프로젝트의 값이어야 하며 개발용 fallback은 없다.

Worker 세션은 `record:false`로 시작하고 LiveKit Cloud의 추가 녹음·transcript 저장을 활성화하지 않는다.
Port의 기존 STT와 대화 기록은 유지한다. 로컬 전화/SIP 스택은 제공하지 않으며 외부 전화 연결은 현재 범위에 없다.
Worker 이미지에는 native RTC의 Cloud TLS 인증서 검증에 필요한 시스템 CA 인증서가 있어야 한다.

Cloud webhook 설정의 endpoint는 `https://<PUBLIC_API_DOMAIN>/api/v1/livekit/webhooks`이며,
서명용 API key는 API에 설정한 것과 같은 LiveKit Cloud 프로젝트의 key를 선택한다.
현재는 공개 HTTPS API 주소가 없어 Cloud의 자동 webhook 전송을 설정하거나 검증하지 않았다.
Railway와 공개 호스팅은 별도 배포 범위이며, 로컬 수신 검증만으로 외부 전송이 준비됐다고 보지 않는다.

API의 `WEB_ORIGIN`은 인증 리다이렉트에 사용하는 canonical HTTPS ingress origin이다.
`WEB_ALLOWED_ORIGINS`는 canonical origin 외에 CORS와 CSRF 검사에서 허용할 정확한
origin을 쉼표로 구분한 목록이며, 단일 ingress example과 현재 Mac 배포에서는 비워 둔다.
개발용 추가 origin은 명시적으로 승인한 경우에만 설정한다. 다른 localhost 포트나
wildcard를 자동 허용하지 않으며 과거 직접 Web HTTP origin은 호환 경로로 유지하지 않는다.
허용 목록은 Origin 검사를 비활성화하지 않으며, CSRF 보호 요청에 Origin이 없으면 거부한다.

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

## 연결과 health 검증

로컬 Compose는 `adaptor-api-tls` Caddy proxy가 API 앞에서 내부 CA 기반 HTTPS를 제공한다.
Adaptor는 해당 CA를 `NODE_EXTRA_CA_CERTS`로 신뢰하고
`https://adaptor-api-tls/api/v1/access-tokens/identity`에서 PAT identity를 검증한다.
`PUBLIC_BASE_URL`은 `http://macbookpro:3002`로 유지한다.

`make health`는 앱의 HTTP liveness와 Worker의 LiveKit Cloud 등록 readiness를 확인한다.
Worker의 Compose healthcheck와 Make 명령은 컨테이너 내부 SDK endpoint `http://127.0.0.1:8081/`을 사용한다.
등록이 완료되고 WebSocket이 연결된 상태에서 HTTP 200을 반환하며, 연결되지 않으면 503을 반환한다.
이 포트는 호스트에 공개하지 않는다. 기존 `19091/metrics`는 관측용이며 Cloud 등록 검사를 대신하지 않는다.
Native auth, Web→API, API→RAG 연동도 별도 manual smoke로 확인한다. Aggregator는 distroless
이미지라 컨테이너 healthcheck 대신 host smoke (`3001/healthz`)를 사용한다.

로컬 Compose의 API는 `NODE_ENV=local`을 사용한다. SMTP가 설정되지 않은 로컬에서
회원가입 이메일 인증 코드를 응답으로 반환하고 Web이 자동 입력할 수 있도록 하는 개발 전용 설정이다.
실제 환경에서는 SMTP를 설정하고 `NODE_ENV=production`으로 실행해야 하며, 인증 코드를
응답이나 로그로 노출하면 안 된다. `MAIL_HOST`, `MAIL_USER`, `MAIL_PASS`는 이 로컬 설정에 넣지 않는다.

Manual smoke checklist: native auth → Web→API→RAG→Voice→LiveKit Cloud 순서로
로그인, API 호출, RAG 요청, Voice bootstrap, Cloud room의 데이터·오디오 송수신을 확인한다.

2026-10-01 LiveKit Cloud 전환 검증:

- API와 Worker만 재생성한 뒤 두 컨테이너가 Healthy이고, 실제 배포 Worker의 내부 `8081/`이 HTTP 200을 반환했다.
  양쪽 접속 주소는 `wss://dubu-mkgk6hgc.livekit.cloud`이며 배포 API의 인증된 Cloud `listRooms` 호출이 성공했다.
- 실제 Worker 이미지의 RTC smoke에서 Cloud 참가자 2명, reliable data 전달, 16kHz의 무음이 아닌 오디오 수신을 확인했다.
  room recording은 비활성 상태였고 세션 시작은 명시적인 `record:false`를 사용한다.
  검증 room을 정리한 뒤 남은 smoke room은 0개였다. 이 결과는 전체 STT/AI 통화나 외부 전화 검증을 의미하지 않는다.
- 실제 배포 API의 `application/webhook+json` 요청은 올바른 서명에서 HTTP 200, 변조·서명 누락에서 401을 반환했다.
  Cloud에서 공개 endpoint로 보내는 전송 경로는 검증하지 않았다.
- 전환 중 다른 컨테이너 11개와 기존 이미지는 유지했다. API는 `port-api:livekit-cloud-fe95496`,
  Worker는 `port-voice-agent:livekit-cloud-tls-606796d`를 사용하며 마지막 이미지 overlay에 고정했다.

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
