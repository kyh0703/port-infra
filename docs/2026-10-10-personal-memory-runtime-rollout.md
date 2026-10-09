# 개인 자동기억·최신 main Mac runtime 운영 전환 완료

기준일: 2026-10-10 KST. 실제 대상은 `colima`, Compose `infra`, PostgreSQL `port`다.
소스·이미지 발행과 별도로 아래 결과를 실제 운영에서 확인했다.

| 역할 | 실제 운영 source | 불변 GHCR digest |
| --- | --- | --- |
| API | `7942f817252c574ec34ce501c78ffb9aa14999eb` | `sha256:a146c2084f1b165b3a3713fbb504d63922bcae7e24a87e3d56f67bc4e87a97ab` |
| api-migrator | 위 API와 동일 | `sha256:badea31aab247f6777998b7e72e218adae8b92d653a2d06d530b98b5c6d53b88` |
| worker | `6c9cc376654e9bca4dd45cc1ca13d1bbddca5546` | `sha256:753dd449deb68a0fafa9a22e7e26858667dd38a34744a5d203f21ec366cf10e4` |

API·worker·RAG·Web·OpenBao가 healthy·restart0이다. RAG의 기존 image와 실제
environment·volumes·networks를 유지하면서 공유 내부 인증 키만 교체했다.
별도 세션의 Web `space-ui-20261009-d44b33bc6433` 배포는 보존했다.
Worker는 실제 production launcher와 포트8000/8081/9091로 실행한다.

## 실제 전환과 데이터 보존

- 기존 보호 자료의 원래3-of-5 quorum으로 격리 cold-Raft 복원본의 관리자 복구와
  실제 AppRole 암호화를 검증했다. 운영 Raft·audit를 cold backup한 뒤 내부 loopback
  전용 복구 경로를 잠시 사용했다. 기존 static seal·cluster·KEK·PII DEK·envelope를
  유지하고 데이터용 non-exportable Transit key와 AppRole ACL을 준비했다.
  임시 root 폐기403과 listener 제거, 원래 TLS·port·image·command 복원을 확인했다.
- 기존 API·worker·RAG를 중지한 뒤 새 custom-format DB backup을 만들고 실제 tmpfs
  복원본과 이력168·사용자5명·legacy47개·사용자 전체 행 지문을 대조했다.
- 정상 owner35개는 실제 구버전 도메인 종료 경로로 정리했다. 기존 pending13개와
  추가 `LegacyOrphanSessionArchive`1개를 immutable migrator로 적용했다.
- Owner fence를 유지한 관리자 경로로 orphan pending12개의 전체 원본을 암호화
  보존하고 routing row를 정리했다. 실제 운영 archive12개 모두 복호화와 원본
  SQL SHA-256 일치를 다시 확인했다.
- 운영 이력은 **168→182**, canonical181개와 별도 historical namespace1개다.
  Pending·legacy active/pending은0이다. 사용자5명·Space1개·원래 금융/usage 데이터를
  보존했다. Ledger3개·usage27개의 추가 column 때문에 전체 JSON 지문이 달라졌지만,
  원래 column만 투영한 모든 원래 행은 frozen backup과 동일했다.

## 실제 권한·admission·기억 검증

- 기존 OpenAI·OpenRouter 호출 키와 voice credential AES material은 유지했다.
  최신 계약은 허용된 speech/model 키를 계속 사용한다. 초기에 관리 키까지 요구한
  범위를 정정했다. 실제 교체 대상은 LiveKit·공유 API/RAG 접근 키다.
- 기존 LiveKit key는 공식 CLI self-revocation 방식의 실제200 후 RoomService401로
  거절됐다. 같은 project의 replacement는 정상 조회됐다. 공유 키는 실제 API401→200,
  RAG401→인증 후 router404로 폐기·교체를 확인했다. Private primary env에도 새 값을 보존했다.
- 실제 Cloud assignment/job/dispatch/room SID·stale incarnation 거부와 소유 identity2개의
  refreshed JWT reconnect401, 두 단계 ACK, authoritative Pong·API VM clock을 결속했다.
- 실제 DB legacy census5항목 모두0인 상태에서 first-cutover ID/hash를 검증했다.
  Pool `infra-native-v8`의 실제 launcher 등록, closed ACK1→revision2→native ACK2를
  확인했다. Initial·recovery admission은 true, 실제 worker `/readyz`는200이다.
- SDK·builtin code digest의 bare64hex와 API 검사 간 시작 오류를 수정했다.
  관련71개 회귀·typecheck·build·lint와 실제 발행 image의 환경 검사가 통과했다.
- 실제 정식 session service의 10분 검증 family로 memory GET200·PATCH200, 실제
  `vault:` DB 저장·재조회 복호화·기존 스타일 복원을 확인했다. 검증 family를 폐기하고
  `/auth/me`200→401도 확인했다. 검증 후 memory는 기본 빈 내용과 활성 상태다.

## 증거와 운영 유지

보호 증거는 ignored `data/runtime-full-main-20261008T084918Z/final-cutover/`다.
조정 파일에는 운영 이력182·native admission·기억 HTTP 검증을 기록하고
추가 writer의 `liveUpAllowed`를 false로 내렸다. 소유 restore/probe container는 제거했다.

재기동은 서비스별 실제 labels의 ordered Compose chain·private runtime env·evidence
mount를 보존한다. RAG의 달라진 base 선언은 실제 설정 보존 override로 고정했다.
이 chain을 생략하는 전체 `make deploy`나 구버전7.x 복귀는 호환 재기동 경로가 아니다.

Cloud capability evidence는 **2026-11-08T14:32:28Z**에 만료된다. 그전에 같은 실제
Cloud 관찰과 artifact hash·API pin 갱신이 필요하다. Worker build·SDK·builtin·codec
변경 시에도 새 evidence가 필요하며 admission guard를 우회하지 않는다.

단일 Mac/Colima 전환이다. 실제 외부 전화·유료 음성 전체 통화, 멀티 노드 HA·RPO0는
이번 완료 검증에 포함하지 않는다.
