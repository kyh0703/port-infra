---
feature: local-email-verification-debug
created_at: 2026-08-31T09:25:23+09:00
---

# Local Email Verification Debug

## Goal

Allow local Compose signup without SMTP by exposing the API-generated verification code only in the explicit local runtime.

## Context / Inputs

- Source docs: `docs/STATE.md`, `docs/ROADMAP.md`, `docs/ARCHITECTURE.md`
- Existing system facts: The API permits `AUTH_EMAIL_VERIFICATION_EXPOSE_DEBUG_CODE=true` only when `NODE_ENV` is development, local, or test. Compose uses a production runner image but a dev-only env example.
- Bug brief: Signup verification returns 503 because local Compose has no SMTP and the debug code is disabled.
- Reproduction: `POST /api/v1/auth/email-verification/send` returns `auth.email_verification_delivery_unavailable` in the current stack.
- Expected vs Actual: Local signup should return and auto-verify a debug code; currently it cannot proceed.
- Suspected Cause: `config/api.env.example` omits `NODE_ENV=local` and sets debug-code exposure to false.
- Regression Risk: Accidentally enabling debug codes outside local Compose or committing SMTP credentials.

## Plan Handoff

### Scope for Planning

- Mark the tracked dev-only API env example as `NODE_ENV=local`.
- Enable email verification debug-code exposure only in that local env example.
- Add repository checks documenting that production/SMTP secrets remain absent.
- Recreate only the API container and smoke the send/verify/register flow.

### Success Criteria

- Local API starts with validated local env.
- Verification send returns a debug code without SMTP and Web can auto-verify it.
- No mail host, user, password, or production secret is introduced.
- Compose tests and health checks pass without replacing named volumes.

### Non-Goals

- Configuring SMTP or changing production deployment settings.
- Disabling verification or bypassing code validation.
- Resetting users or deleting local data.

### Open Questions

- None.

### Suggested Validation

- RED/GREEN shell assertion for local NODE_ENV/debug flag.
- `make test`, Compose config validation, API health.
- Local signup send/verify/register smoke with a temporary unique email.

### Parallelization Hints

- Candidate write boundaries: local API env example and infra contract test.
- Shared files to avoid touching in parallel: `config/api.env.example`.
- Likely sequential dependencies: env contract test, config change, Compose smoke.
