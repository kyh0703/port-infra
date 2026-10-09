# 개인 기억·최신 main Mac runtime 전환 준비

기준일: 2026-10-09. 실제 대상은 Docker context `colima`, Compose project
`infra`, PostgreSQL `port`다. 사용자는 전체 최신 main runtime을 선택했으며
통합 준비 후 migration을 적용하도록 결정했다.

## 실제 확인 결과

| 항목 | 관찰 |
| --- | --- |
| LiveKit project 인증 | 기존 `overthinker` project 승인, 실제 RoomService 조회 성공 |
| Native assignment | 새 SDK worker·job·dispatch·room SID 결속, 오래된 incarnation 거부 |
| Token refresh revocation | 소유 identity2개, 갱신 JWT 실제 reconnect401 거부, 두 단계 ACK |
| Provider clock | 실제 signal Pong의 server timestamp·송신·수신 시각 2개 관찰 |
| Migration SQL | 실제 PostgreSQL orphan 정리 회귀7개 통과 |
| Backup restore | 보호 backup의 격리 복원에 기존13개+추가관리1개 UP 성공, ledger168→182 |
| 운영 AppRole | login200, 새 private-data encrypt403, token revoke-self204 |
| 공급자 관리 키 | 일반 OpenAI 키의 관리403, OpenRouter 관리401; 두 일반 서비스 키는 유효 |
| 운영 전환 | 미실행. 운영 DB168개, 기존 API·worker 유지 |

실제 Cloud job readback은 embedded `job.room`을 생략한다. API는 원래 SDK의
assignment SID를 현재 RoomService와 정확한 dispatch/job/worker 식별자로
검증하도록 수정했다. Default absent remove의404는 긍정 ACK가 아니므로 첫
revocation에도 명시적 cutoff를 사용한다. 수정 회귀12개·typecheck·build·lint 통과.

Probe scripts는 `runtime_cloud_assignment_probe.mjs`,
`runtime_cloud_revocation_probe.mjs`, `runtime_native_probe_parent.mjs`,
`runtime_native_probe_agent.mjs`다. Root project key는 부모에서만 사용하고
child에는 소유 room에 한정된 token을 전달한다. Production conversation,
LLM, speech, tool을 실행하지 않는다. Room과 probe worker는 정리했다.
이 결과는 운영 first-cutover, 원본 key identity 보존, 실제 전체 통화의 증거가 아니다.

보호 자료는 ignored `data/runtime-full-main-20261008T084918Z/`에 보관한다.
`migration-coordination.sanitized.json`은 단일 rollout/migration writer와
`liveUpAllowed:false`를 기록한다. Auth URL, key 값, JWT와 사용자 원본은
이 문서나 tracked script에 넣지 않는다.

## 실행하지 않은 Compose 후보

실제 API32개·env2개, worker19개·env1개의 ordered labels를 읽어 후보를
render했다. Baseline의 선언된 environment는 현재 컨테이너와 동일하다.
후보의 변경은 API의 image·environment, worker의 image·environment·command·
healthcheck뿐이다. 기존 volumes·ports·networks와 최신 Web 배포를 보존한다.

Worker는 발행된 `6c9cc376` 불변 image와 baked inventory를 사용한다.
`readRuntimeLauncherConfig`를 실제 image에서 실행해 target·inventory·model
cache·key 길이·포트8000/8081/9091 검사가 통과했다. Worker를 등록하지 않았다.
승인된 실제 project ID와 새 project key를 후보에 결속했고, 기존 API의 Redis
endpoint를 새 parent에 명시했다. 과거 후보의 URL hostname을 project ID로
사용하지 않는다.

후보 파일은 owner-only이며 운영에 적용하지 않았다. Capability/first-cutover
artifact와 공급자 교체·암호화 proof는 아직 결속하지 않았다. API main
`4b6e538b`의 CI image 발행과 digest 확인도 최종 적용 전에 확인한다.

## 남은 필수 작업

1. OpenAI Admin Key·OpenRouter Management Key 또는 실제 운영자의 새 키·폐기
   결과를 받아 old-denied/new-accepted를 확인한다. 일반 서비스 키로 관리 권한을
   대신하지 않는다.
2. 승인된 OpenBao operator principal로 기존 AppRole의 private-data ACL과
   non-exportable data/lookup key를 준비한다. 기존 seal·KEK·DEK·manifest는
   보존한다. `scripts/openbao-private-data.py`의 기본 dry-run 결과를 먼저 확인한다.
3. 실제 운영 key로 암호화 원본 보존 rehearsal을 통과하고, 정상 owner35개는
   기존 실제 도메인 종료 경로로, owner 없는 pending12개는 새 관리자 archive
   경로로 정리할 준비를 한다. SQL 상태 변경·owner fence 해제로 대체하지 않는다.
4. 서비스별 최신 container labels의 Compose/env 순서를 복원한다. API·worker에
   새 launcher/control/target/health 설정과 불변 이미지를 준비한다. 별도 세션이
   올린 최신 Web 및 관련 override는 유지한다.
5. 호환 replacement와 native·credential·preservation 자료가 준비되면 쓰기를
   동결하고 새 backup을 복원 검증한다. 단일 writer가 original13개와 추가관리1개
   migration, 정리, API·worker 전환과 실제 memory 통화 검증을 함께 실행한다.

DB 단독 UP은 기존 API가 사용하는 `llm_audit_executions.capability_hash`를
없애 호환성을 깨뜨린다. 현재 user 결정은 migration-only 서비스 중단의 승인이
아니다. Main 소스·이미지 발행이나 local SQL 통과를 운영 배포 완료로 기록하지 않는다.
