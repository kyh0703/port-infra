# Compose Development Runtime

## Goal

- Preserve the default image-based Compose runtime and add an explicit API/Web development override with source watch.

## References

- docs/AGENTS.md
- docs/STATE.md
- docs/ROADMAP.md
- docs/ARCHITECTURE.md
- docs/v1/designs/2026-08-31-v1-compose-development-runtime.md

## Workspace

- Branch: fix/v1-compose-development-pnpm-store
- Base: main
- Isolation: required
- Created by: exec-plan via git-worktree

## Task Graph

### Task T1

- [x] Complete
- Goal: Add and deploy an explicit API/Web development Compose override while preserving the default runtime contract.
- Depends on:
  - none
- Write Scope:
  - compose.dev.yml
  - Makefile
  - README.md
  - tests/compose.sh
  - tests/commands.sh
- Read Context:
  - compose.yml
  - ../api/Dockerfile
  - ../api/package.json
  - ../web/Dockerfile
  - ../web/package.json
  - ../web/server.js
  - docs/v1/designs/2026-08-31-v1-compose-development-runtime.md
- Checks:
  - make test
  - docker compose -f compose.yml -f compose.dev.yml config --quiet
  - make dev-up
  - manual: API and Web containers run development watch commands with NODE_ENV=development and both health endpoints respond
  - git diff --check
- Parallel-safe: no

### Task T2

- [x] Complete
- Goal: Keep pnpm's content-addressable store out of the API and Web host worktrees during dev startup.
- Depends on:
  - T1
- Write Scope:
  - compose.dev.yml
  - tests/compose.sh
- Read Context:
  - ../api/.npmrc
  - ../web/.npmrc
  - docs/v1/designs/2026-08-31-v1-compose-development-runtime.md
- Checks:
  - make test
  - docker compose -f compose.yml -f compose.dev.yml config --quiet
  - manual: dev startup uses named pnpm store volumes and leaves API/Web git worktrees clean
  - git diff --check
- Parallel-safe: no

## Notes

- Do not add dynamic build-context variables or modify sibling application repositories.
- Do not delete or recreate persistent data volumes.
- The development override applies only to API and Web.
