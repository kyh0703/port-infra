---
feature: compose-development-runtime
created_at: 2026-08-31T10:15:00+09:00
---

# Compose Development Runtime

## Goal

기존 이미지 기반 Compose는 유지하면서 API와 Web만 source watch가 가능한 명시적 개발용 Compose override로 실행한다.

## Context / Inputs

- Source docs: `docs/STATE.md`, `docs/ROADMAP.md`, `docs/ARCHITECTURE.md`
- Existing system facts: 기본 `compose.yml`은 GHCR `:dev` 이미지의 production-built 프로세스를 실행한다. API 컨테이너는 `NODE_ENV=local`, Web 컨테이너는 `NODE_ENV=production`이며 둘 다 source watch는 하지 않는다. API와 Web Dockerfile에는 개발 의존성을 가진 `deps` target이 있다.
- User brief: 기본 실행 경로와 개발 실행 경로를 분리하고 현재 로컬 Compose는 dev hot-reload 모드로 올린다.

## Plan Handoff

### Scope for Planning

- `compose.dev.yml`에서 API와 Web만 sibling source context의 기존 `deps` target으로 빌드한다.
- 필요한 source/config 파일만 bind mount하고 API는 `pnpm dev`, Web은 기존 custom proxy를 보존하는 `pnpm dev`로 실행한다.
- Makefile에 dev up/log/stop 명령을 추가하고 기본 Compose 동작과 named volume은 변경하지 않는다.
- 기본/개발 Compose 렌더링 계약과 금지된 동적 build-context 변수 부재를 테스트한다.
- 개발 override로 API/Web을 실제 재생성하고 `NODE_ENV=development`, watch 프로세스, health를 확인한다.

### Success Criteria

- `docker compose -f compose.yml -f compose.dev.yml config --quiet`가 통과한다.
- 기본 Compose의 API/Web image-only 계약은 유지된다.
- 개발 override는 API/Web에 고정 sibling build context와 `deps` target, `pnpm dev`, source bind mount를 적용한다.
- `make dev-up`으로 API/Web이 development 환경에서 healthy 상태가 되고 로그인/회원가입 화면과 API health가 응답한다.
- source 수정은 이미지 재빌드 없이 watcher가 감지할 수 있다.

### Non-Goals

- RAG, Voice Agent, Aggregator, Adaptor의 개발 모드 전환.
- production 배포 방식 변경.
- named volume 삭제 또는 secret 추적.
- `API_BUILD_CONTEXT` 같은 동적 context 환경변수 추가.

### Open Questions

- 없음. 기본 Compose는 보존하고 개발 override를 명시적으로 선택한다.

### Suggested Validation

- `make test`
- `docker compose -f compose.yml -f compose.dev.yml config --quiet`
- `make dev-up`
- 컨테이너 command/env inspect 및 API/Web health smoke
- `git diff --check`

### Parallelization Hints

- Candidate write boundaries: Compose/test 계약, Makefile/README runbook.
- Shared files to avoid touching in parallel: `tests/compose.sh`, `Makefile`, `README.md`.
- Likely sequential dependencies: Compose override 계약 확정 후 runbook과 실제 runtime smoke.
