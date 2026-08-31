# Local Email Verification Debug

## Goal

- Restore SMTP-free local signup while keeping debug verification strictly scoped to the local Compose runtime.

## References

- docs/STATE.md
- docs/ROADMAP.md
- docs/ARCHITECTURE.md
- docs/v1/designs/2026-08-31-v1-local-email-verification-debug.md

## Workspace

- Branch: fix/v1-local-email-verification-debug
- Base: main
- Isolation: required
- Created by: exec-plan via git-worktree

## Task Graph

### Task T1

- [x] Complete
- Goal: Enable and verify local-only debug email verification in the tracked Compose API env contract.
- Depends on:
  - none
- Write Scope:
  - config/api.env.example
  - tests/compose.sh
  - README.md
- Read Context:
  - ../api/src/configs/env.validation.ts
  - ../api/src/modules/auth/email-verification/email-verification.service.ts
- Checks:
  - make test
  - docker compose -p infra config --quiet
  - make health
  - manual: verification send/verify/register succeeds with a temporary local email
- Parallel-safe: no

## Notes

- Never add SMTP credentials or modify named volumes.
- The API image remains production-built; `NODE_ENV=local` is an explicit runtime override for this dev-only Compose stack.
