# Personal Space 런타임과 배포 경계

이 문서는 운영자가 Personal Space의 저장·발행·실행 책임을 구분하고, 기존 서비스와 데이터를 보존하면서 Web UI만 교체하도록 안내한다. 제품 개념과 용어의 기준은 [Personal Space 개념 명세](../../spec/docs/personal-space-concepts.md)다. 이 문서는 새로운 서비스, 예약 API, DB migration을 도입하지 않는다.

기준일은 **2026-10-04 full-canvas 릴리스**다. 다음 상태를 구분한다.

- **현재 구현·관측**: 해당 소스나 날짜가 명시된 실행 기록으로 확인한 범위다. 이후 배포 상태까지 보증하지 않는다.
- **합의된 방향**: 제품이 지향하는 책임과 사용자 흐름이다. 구현 완료를 뜻하지 않는다.
- **설계 제안**: 저장 모델·실행 정책 등 별도 설계와 구현이 필요한 내용이다.
- **미검증**: 이번 릴리스에서 실제 동작을 확인하지 않은 경로다.

## 1. Save, UI Deploy, 컨테이너 배포

Personal Space는 사용자의 삶과 업무 맥락을 담는 캔버스다. note·goal·rule 원본 하나를 여러 area에서 참조하며, area는 맥락 범위이지 Agent 복제본이 아니다. Supervisor와 specialist는 실행 그래프의 역할이다. area나 specialist를 추가할 때마다 Compose 서비스나 컨테이너를 만드는 모델이 아니다.

| 동작 | 책임과 결과 | 바뀌지 않는 것 |
| --- | --- | --- |
| 캔버스 편집 | Web이 원본, 참조, 실행 노드, 배치를 편집한다. 현재 UI는 전체 캔버스·하단 floating 도구·선택적 overlay 편집기를 제공한다. | 저장 전 편집은 서버의 실행 publication을 바꾸지 않는다. |
| **Save** | Web이 API에 draft 저장을 요청한다. API가 소유자와 revision을 확인하고 PostgreSQL에 저장한다. | 이미 발행한 immutable publication과 실행 중인 컨테이너는 바뀌지 않는다. |
| **UI Deploy** | 미저장 변경을 먼저 저장한 뒤 API가 실행 가능한 immutable publication을 만든다. 저장 실패 시 발행하지 않는다. | Docker build/pull/recreate나 서버 소프트웨어 배포가 아니다. |
| **컨테이너 rollout** | 운영자가 검증한 이미지로 특정 프로세스를 교체한다. | 사용자의 draft를 저장하거나 Agent/Space를 자동 발행하지 않는다. |

UI의 변경 표시는 **최신 활성 publication**과 draft의 실행 의미를 비교한다. 과거 publication을 열어 본 선택 상태나 마지막 Save가 기준을 바꾸지 않는다. 좌표·viewport 같은 배치만의 변경은 실행 변경에 포함하지 않는다. 발행 실패 후 저장된 draft와 미발행 변경은 남을 수 있다.

근거: [Web full-canvas·deployment indicator 기록](../../web/docs/STATE.md), [Spaces controller](../../api/src/modules/spaces/spaces.controller.ts), [저장·발행 repository](../../api/src/modules/spaces/spaces.repository.ts). API 링크의 기준 소스는 `42e3e93ff9f050ecc093ee710b9174893467feb3`이며, 다른 branch의 로컬 checkout을 이 배포의 소스로 간주하지 않는다.

## 2. 프로세스별 책임

| 구성 요소 | 현재 책임 | 예약 앱 수신 통화와의 경계 |
| --- | --- | --- |
| **Web** | Space authoring, Save/Deploy 요청, publication 이력과 인증된 테스트 진입을 제공한다. HTTP API는 같은 origin의 `/api/v1`로 요청한다. | 브라우저 탭·타이머가 예약의 실행 주체가 아니다. 현재 full-canvas UI에 예약 설정이 구현됐다는 뜻도 아니다. |
| **API + PostgreSQL·Redis** | API가 인증·소유자 확인, draft revision, publication 생성, 실행 진입을 제어한다. PostgreSQL은 Space·publication 등 영속 데이터를, Redis는 기존 인증 세션과 런타임 상태를 담당한다. | 예약 정의·발생분별 실행 기록을 DB에 보관하고 서버 API/worker가 due run을 깨우는 것은 **설계 제안**이다. Redis timer나 클라이언트 상태만을 예약의 원본으로 삼지 않는다. |
| **voice-agent** | LiveKit에 등록된 worker가 발행된 실행 그래프와 세션의 대화·도구 실행을 처리한다. Supervisor와 specialist는 이 런타임의 역할이다. | LLM이 다음 날까지 대기하거나 worker가 사용자별로 sleep하는 방식이 아니다. 예약을 깨우는 서버 작업과 통화에 참여하는 voice worker를 구분한다. |
| **LiveKit Cloud** | 현재 미디어 연결과 room/participant 전송 경로를 제공한다. 로컬 Compose에 새 미디어 서버를 추가하는 전제가 아니다. | 예약 저장소·앱 푸시 발송자·사용자 수락 정책을 대신하지 않는다. |
| **native push client** | iOS·Android에 로그인·세션 처리 기반이 있다. iOS에는 로컬 기기 식별자 준비도 있다. 이를 서버 기기 등록이나 수신 준비 완료로 취급하지 않는다. | 푸시 초대 표시, OS 통화 UI, 사용자 수락 후 LiveKit 연결은 목표다. APNs/FCM → 실제 기기 수신 → 수락 → 음성의 전체 경로는 배포·검증 완료가 아니다. |
| **OpenBao** | API의 비밀 접근과 암호화 키 경계를 지원한다. 데이터·audit·credential volume과 CA/AppRole 연결을 보존한다. | 예약 저장소나 실행 엔진이 아니다. 키·토큰을 Web, publication 본문, 문서에 복사하지 않는다. |

서비스 연결과 저장소는 [Compose](../compose.yml), secret 접근은 [OpenBao 운영](../README.md#openbao-비밀-저장소)을 따른다. 모바일의 구현 범위는 [iOS 계획 §13](../../native-ios/docs/PLAN.md), [Android 계획 §10](../../native-android/docs/PLAN.md)을 따른다. 계획서 앞부분의 수신 시나리오를 현재 제공 기능으로 읽지 않는다.

### 현재 실행 경로

사용자가 저장한 Space를 발행하면 기존 execution publication 경계를 통해 Supervisor snapshot으로 연결된다. 인증된 publication 테스트는 기존 API·voice-agent·LiveKit Cloud 경로를 사용하며, Space 전용의 두 번째 실행 엔진이나 숨은 Agent를 만들지 않는다.

[2026-10-02 Spaces Supervisor 배포 기록](../../web/docs/STATE.md)에는 실제 Cloud RTC와 Supervisor 응답, specialist 작업 완료가 기록돼 있다. 이는 **사용자가 시작한 publication 테스트의 과거 증거**이며, 2026-10-04 UI 교체에서 통화 경로를 다시 검증했다는 의미는 아니다. 같은 기록에서 phone·public WebChat의 production pointer는 Agent 전용이다. Space publication 생성만으로 예약 발신이나 모든 채널의 연결이 활성화되지 않는다.

## 3. 예약 앱 수신 통화: 합의된 방향과 설계 제안

예시는 “매일 오전 9시에 전화해서 오늘 할 일 정리해 줘”다. 이는 프롬프트를 실행 중인 LLM에 남겨 기다리게 하는 요청이 아니라, **소유자 + Space + 실행 노드 + 지시 + `Asia/Seoul` 시간 규칙**으로 식별할 예약 의도다.

다음 흐름은 **미배포 설계 방향**이다.

1. 사용자가 실행할 Space·노드와 지시, 시간 규칙을 지정한다.
2. 서버가 예약 정의를 영속 저장하고, 각 예정 시각의 실행을 별도 발생분으로 식별한다.
3. 서버 API/worker의 예약 처리부가 도래한 발생분을 선택한다. 어떤 프로세스로 구성할지는 별도 설계 대상이며, 현재 그런 서비스가 실행 중이라는 뜻이 아니다.
4. API 측에서 소유자와 수신 기기 권한을 확인한 앱 초대를 전달한다.
5. native client가 OS 정책에 맞게 수신을 표시하고 사용자의 수락을 받는다.
6. 수락이 승인된 세션이 LiveKit Cloud에 연결되고 voice-agent가 해당 실행을 수행한다.

**DB 예약 정의와 발생분별 run 모델은 제안이며, 확정된 테이블·API 계약이 아니다.** 실행 시 어느 publication을 참조할지, 취소·만료·중복 발생분의 처리, 여러 기기의 수락 경쟁, 푸시 제공자 설정은 별도 설계·구현·검증이 필요하다. 실제 발생분에서 사용할 publication과 실행 노드를 식별할 수 있어야 한다. Agent settings에 배치하는 방안도 선택적 개념 설계이지 현재 UI의 제공 기능이 아니다.

운영 경계는 다음과 같다.

- LLM의 시간 계산·sleep, 열린 브라우저의 타이머를 스케줄러로 사용하지 않는다.
- 현재 기준 소스에서 **outbound campaign과 전용 polling/scheduling worker는 제거됐다**. 과거 campaign scheduler 기록은 역사적 참고이며, 이를 가동 중인 예약 기반으로 제시하거나 별도의 중복 campaign 서비스를 추가하지 않는다. 근거: [API 제거 기록](../../api/docs/STATE.md).
- 앱 수신이 실패하거나 사용자가 받지 않았다고 일반 PSTN 전화로 자동 전환하지 않는다.
- 전송·발신 결과가 불명확한 상태에서 부작용을 반복하는 자동 재시도를 전제하지 않는다. 동일 발생분의 상태와 중복 실행 방지 정책을 먼저 정의한다.
- native 로그인 성공, SDK 포함, LiveKit worker 등록, 컨테이너 health만으로 앱 수신 통화의 완료를 주장하지 않는다.

## 4. 실행 환경과 Compose 체인 조사

이 릴리스의 실행 환경은 **로컬 Mac/Colima의 Compose project `infra` + LiveKit Cloud**다. 원격 Kubernetes/GitOps 배포 기록이 아니다. 일반 운영 절차는 [spec의 Compose 운영](../../spec/operations/compose.md)을 함께 읽되, 그 문서의 이전 날짜 포트·미디어 구성보다 현재 컨테이너와 릴리스 기록을 우선한다.

### 서비스별 기록 복원

변경 전 아래와 같이 읽기 전용으로 실제 대상과 labels를 확인한다. 명령은 운영자용 안내이며 이 문서 변경 과정에서 실행한 배포 절차가 아니다.

```bash
docker context show
docker compose ls
docker ps --filter label=com.docker.compose.project=infra \
  --format '{{.ID}} {{.Names}} {{.Image}} {{.Status}}'
docker inspect infra-web-1 --format '{{json .Config.Labels}}'
docker inspect infra-web-1 \
  --format 'image={{.Image}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}}'
```

- Web 컨테이너의 `com.docker.compose.project`, `com.docker.compose.project.working_dir`, `com.docker.compose.project.config_files`와 환경파일 기록을 기준으로 원래 실행 인자를 복원한다. 환경파일 label이 없으면 기존 운영 실행 기록으로 확인한다. 파일이나 순서를 추측하지 않는다.
- `compose ls`는 다른 서비스의 override까지 함께 보여줄 수 있다. 전체 프로젝트의 파일 목록을 Web에 일괄 적용하지 않고 **Web 자신의 ordered file chain**을 사용한다.
- `-f`와 `--env-file` 모두 뒤의 설정이 앞의 값을 덮어쓸 수 있다. 기존 project directory와 파일 순서, env-file 순서를 그대로 유지한다. CLI의 `--env-file`과 서비스 내부 `env_file`도 서로 대체하지 않는다.
- 설정과 mount의 기준 경로는 기존 운영 infra다. 문서/개발용 worktree를 배포 working directory나 bind mount로 끼워 넣지 않는다.
- 전체 `docker inspect`나 해석된 Compose config에는 secret 환경값이 포함될 수 있다. 운영자 전용 범위에서 비교하고 원문을 Git·문서·공개 로그에 남기지 않는다.

### HTTP API와 same-origin ingress

브라우저의 HTTP API 요청은 Web과 **같은 origin의 `/api/v1`**로 보낸다. [Caddy 라우팅](../ingress/Caddyfile)은 정확한 `/api/v1` 및 `/api/v1/*`를 API로, 나머지를 Web으로 전달한다. Web의 Next rewrite를 되살리거나 별도 브라우저 API origin을 만들지 않는다.

same-origin은 외부 접근을 평문 HTTP로 바꾸라는 뜻이 아니다. 현재 [trusted ingress 운영](../README.md#webapi-trusted-ingress)은 canonical HTTPS origin과 내부 HTTP upstream을 구분한다. UI-only rollout에서 이 origin, cookie 정책, trusted proxy, 기존 host/Tailnet 연결을 변경하지 않는다. 실제 화면 점검도 내부 Web health 포트만이 아니라 인증에 사용하는 ingress로 수행한다.

## 5. Web만 교체하는 안전한 rollout

적용 조건은 **UI-only 변경이며 API·worker·schema 변경이 없는 경우**다. full-canvas 릴리스가 이 경우에 해당한다.

1. **기준 보관**: Web의 이전 image ID·소스 revision·Compose/env 체인과 비-Web 컨테이너 ID를 기록한다. rollback 이미지 태그와 named volume 목록을 보존한다. 승인된 범위에서 Space 저장 데이터의 변경 전 읽기 결과를 확보한다.
2. **이미지 고정**: 의도한 소스로 빌드된 immutable 이미지를 준비하고 로컬 존재 여부를 확인한다. mutable `:dev`의 최신 값으로 대체하지 않는다. 로컬 image ID와 registry digest를 혼동하지 않는다.
3. **최소 override**: 기존 Web 체인의 끝에 Web 이미지 선택만 바꾸는 local override를 적용한다. 이 릴리스 파일은 `config/space-full-canvas-20261004.local.yaml`이며 `web.image`와 `pull_policy: never`를 가진다. 기존 마지막 파일보다 뒤에 적용하되 같은 override를 중복 추가하지 않는다.
4. **설정 비교**: 원래 공통 인자를 유지한 상태에서 해석된 설정을 비교한다. 이 릴리스처럼 의도한 Web image pin 외에 환경값·명령·mount·network·port·다른 서비스 이미지가 바뀌지 않아야 한다. 차이가 있으면 교체 전에 원인을 해결한다.
5. **서비스 한정 교체**: 복원한 `docker compose` 공통 인자 뒤에 아래 서비스 한정 인자를 붙인다. API·voice-agent·ingress·migrator를 함께 재생성하거나 migration을 실행하지 않는다.

```text
up -d --no-deps --no-build --pull never --wait --wait-timeout 150 web
```

위 줄은 **전체 Compose 명령이 아닌 접미 인자**다. 앞의 project/directory/ordered `-f`/모든 `--env-file` 인자가 빠진 채 실행하지 않는다. 기본 `make deploy`, bare `docker compose up`, 전체 stack 재생성으로 대체하지 않는다. `--no-deps`는 의존 서비스 시작을 막는 옵션이지 현재 환경이나 데이터가 보존됐다는 증거가 아니다.

### 완료 판단

| 확인 범위 | 필요한 증거 |
| --- | --- |
| 프로세스 | Web의 의도한 image ID·소스 revision, healthy와 restart 상태. API·voice-agent·OpenBao·ingress 및 다른 비대상 컨테이너 ID가 유지됐는지 비교한다. |
| 실제 surface | 같은 ingress의 API health뿐 아니라 실제 인증된 Space 화면을 desktop/mobile에서 확인한다. canvas·overlay·가로 overflow와 API 응답을 확인한다. synthetic fixture 결과와 실제 ingress 결과를 구분한다. |
| 데이터 | 기존 Space와 publication의 읽기 결과, DB·credential·cache volume의 보존을 확인한다. 화면 점검을 위해 실제 사용자 데이터에 Save/Deploy를 실행하지 않는다. |
| 범위 | 테스트 통화나 푸시를 실행하지 않았다면 그 경로는 미검증으로 남긴다. worker의 LiveKit 등록 readiness는 앱 푸시 수신 증거가 아니다. |

OpenBao의 Compose health는 sealed 상태도 healthy로 표시할 수 있다. 필요한 secret 접근 readiness는 [OpenBao 운영](../README.md#openbao-비밀-저장소)의 별도 경계를 따른다. HTTP 200이나 모든 컨테이너의 healthy만으로 제품 전체 동작을 보증하지 않는다.

Compose 옵션 근거: [파일 병합과 경로](https://docs.docker.com/reference/cli/docker/compose/), [환경파일 순서](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/), [서비스 교체와 wait](https://docs.docker.com/reference/cli/docker/compose/up/).

## 6. 2026-10-04 full-canvas 릴리스 관측

아래는 해당 릴리스의 관측 기록이다. 새로 실행한 점검이나 항상 유지되는 구성 상수를 뜻하지 않는다. 상세 UI·빌드·브라우저 증거는 [Web STATE](../../web/docs/STATE.md)의 `2026-10-04 Personal Space full-canvas editing` 절에 있다.

| 항목 | 기록 |
| --- | --- |
| 소스 | Web `305e7728`, 이후 main 기록 `11c424a9`. 유지된 API 소스는 `42e3e93ff9f050ecc093ee710b9174893467feb3`. |
| 실행 대상 | 로컬 Mac/Colima Compose `infra`; 미디어는 LiveKit Cloud. 이번 rollout에 registry push나 원격 GitOps 배포는 없었다. |
| Web pin | 로컬 image ID `sha256:3ca8b27ce3be7f61ed37a7fda4af274b0c4011656a602e453d2883262937e742`. |
| Compose 보존 | 기존 **25개 Compose 파일과 2개 환경파일**을 보존한 뒤 full-canvas override를 마지막에 추가했다. 이 숫자는 당시 기록이며 전체 체인을 문서에 고정하지 않는다. 다음 배포는 다시 서비스 labels에서 복원한다. |
| 설정과 재생성 | 해석된 설정은 Web image pin 외에 같았고 **Web만** `--no-deps`로 재생성했다. 비교한 API·voice-agent·OpenBao·ingress 컨테이너 ID는 유지됐다. |
| 상태 | Web·API·voice-agent·OpenBao·ingress가 healthy, restart `0`이었다. |
| 실제 화면 | 실제 인증된 ingress에서 1440px·390px 화면을 확인했다. full-canvas 경계, desktop floating inspector, mobile outline sheet, 가로 overflow 없음이 확인됐다. 실제 사용자 Space를 편집·저장·발행하지 않았고 영속 데이터는 전후 동일했다. |
| 확인 범위 | 별도의 browser-only fixture로 편집·Save/Deploy 시나리오를 확인한 기록과 실제 ingress의 비변경 화면 점검을 구분한다. 이번 UI rollout은 새 통화·예약·native 수신의 검증이 아니다. |

`config/space-full-canvas-20261004.local.yaml`은 [`.gitignore`](../.gitignore)의 `config/*.local.yaml` 규칙에 해당하며 Git 추적 파일이 아니다. fresh clone에 이 파일이나 비공개 env가 들어 있다고 가정하지 않는다. 문서는 image pin과 복원 원칙만 기록하며 비밀값이나 호스트의 사용자별 절대 경로를 복제하지 않는다.

## 7. Rollback과 Docker 정리

### 같은 체인을 유지하는 Web rollback

이번 릴리스는 직전 이미지 `sha256:0800e6f154c23c2729c3c8ff33d2ecc9bbbf250fd33f10a8ffb6a819efd75df5`를 `port-web:rollback-before-full-canvas-20261004` 태그로 보존했다.

1. 이 릴리스가 여전히 마지막 변경인지 현재 Web labels와 image를 확인한다. 후속 릴리스가 있으면 그 변경을 무시하고 과거 체인으로 되돌리지 않는다.
2. 당시 체인에서 **마지막 full-canvas override만 제외**하고 앞선 모든 Compose 파일·환경파일·project directory를 유지한다. 해석된 Web 이미지가 보존한 직전 이미지인지 확인한다. 과거 이미지가 없거나 다른 변경까지 섞이면 임의의 `:dev`를 pull하지 않고 복원 조건부터 해결한다.
3. 같은 서비스 한정 교체 방식으로 Web만 재생성한다. API·worker·DB·OpenBao·ingress와 volume은 유지한다.
4. health, 실제 인증 surface, 비대상 컨테이너와 Space 데이터 보존을 다시 확인한다. Web 이미지 복구는 publication 취소나 DB 복원이 아니다.

이 UI-only 변경에는 DB migration rollback이 없다. 별도 schema 변경이 있었던 릴리스라면 이미지 복구만으로 DB까지 되돌아간다고 가정하지 않고 [spec 운영 복구 원칙](../../spec/operations/compose.md#복구)을 따른다.

### 정리 범위와 실제 회수량

아래 명령은 **이미 실행한 정리의 범위 기록**이지 매 배포마다 실행하는 후처리가 아니다. 정리는 별도 승인과 대상 확인 후 수행한다.

| 명령 | 범위 | 2026-10-04 결과 |
| --- | --- | --- |
| `docker builder prune --all --force` | 선택된 builder의 사용하지 않는 build cache를 정리한다. 컨테이너·named volume·태그된 이미지 삭제 명령이 아니다. 재빌드 cache는 사라진다. | **7.605GB** 회수, 당시 build cache 0B. |
| `docker image prune --force` | 기본 dangling image 정리다. `--force`는 확인 생략이며 `--all`과 다르다. | **0B** 회수. 태그된 과거 이미지와 rollback 이미지는 보존했다. |

정리 전후 **47개 volume과 기존 컨테이너를 모두 보존**했다. volume/system-wide prune은 실행하지 않았다. volume 개수는 해당 시점의 비교값이며 향후 정상 개수로 하드코딩하지 않는다.

- `docker compose down -v`, `docker volume prune`, `docker system prune`을 이 rollout의 정리 방식으로 사용하지 않는다. `system prune`은 volume 옵션이 없어도 중지된 컨테이너 등 정리 범위가 넓다.
- `docker image prune -a`는 컨테이너가 참조하지 않는 태그된 rollback 이미지도 지울 수 있다. 현재 컨테이너가 사용하지 않는다는 이유만으로 불필요한 이미지라고 판단하지 않는다.
- DB, Redis, OpenBao data/audit/credentials, 캐시의 named volume을 공간 확보 대상으로 일괄 제거하지 않는다. builder cache 정리와 영속 데이터 삭제를 구분한다.

명령 범위 근거: [builder prune](https://docs.docker.com/reference/cli/docker/builder/prune/), [image prune](https://docs.docker.com/reference/cli/docker/image/prune/), [system prune](https://docs.docker.com/reference/cli/docker/system/prune/).

## 8. 아직 제공하지 않는 범위

- Personal Space의 영속 예약 정의·발생분별 run·due-run 처리부는 제안이며 배포된 scheduler가 아니다.
- native 기기 등록과 푸시 수신, OS 통화 수락, 실제 LiveKit 음성까지의 연결은 로그인 구현과 별개의 미완료·미검증 범위다.
- Agent settings 기반 예약 UI, 자동 PSTN fallback, LLM timer는 현재 기능으로 제시하지 않는다.
- full-canvas rollout의 성공은 UI·기존 runtime 보존에 대한 기록이다. 앱 수신·예약 실행·외부 전화 또는 원격 배포 성공으로 확대하지 않는다.
