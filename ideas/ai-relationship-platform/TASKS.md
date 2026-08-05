# HeartSignal — Codex Implementation Plan

## How to use this file

Codex should execute milestones in order. Do not start a later milestone while an earlier one lacks its acceptance criteria.

For every milestone:

1. Inspect the repository.
2. State the files to add or change.
3. Implement the smallest complete version.
4. Add tests.
5. Run the relevant checks.
6. Update documentation.
7. Summarize decisions and remaining risks.

## Current active continuation

Milestones 0–8 describe the original MVP plan and their implemented vertical slices. Milestone 5
was subsequently expanded into production billing stages. All planned monetization code slices
through M5D are complete, but Milestone 5 is **not fully complete** until the five live staging
acceptance gates pass for the exact deployed release.

The authoritative continuation checklist is
[`docs/milestone-5-roadmap.md`](docs/milestone-5-roadmap.md):

- [x] M5A — credits, preview and mock payments;
- [x] M5A.1 — monotonic paid report access;
- [x] M5B.1 — production billing foundation;
- [x] M5B.2 — YooKassa/Stripe one-time payments;
- [ ] **M5B.3 — subscription code complete; Stripe and YooKassa sandbox gates open**;
- [ ] **M5B.4 — refund code complete; Stripe and YooKassa sandbox gates open**;
- [ ] **M5C — paid follow-up code complete; OpenAI staging gate open**;
- [x] **M5D — auditable staging release-gate control plane**.

Do not declare the monetization milestone complete based on CI, mock providers or local testing.
Execute the five real staging procedures, record append-only evidence as described in
[`docs/release-gates.md`](docs/release-gates.md), resolve all financial blockers and require
`/admin/release-readiness` to return `ready_for_limited_production=true` before limited launch.

## Milestone 0 — Bootstrap the project

### Goal

Create a reproducible local development environment and a maintainable application skeleton.

### Deliverables

- Python 3.12 project configuration.
- Application package.
- FastAPI app with `/health/live` and `/health/ready`.
- aiogram bot setup with long-polling for local development and webhook-ready configuration.
- PostgreSQL connection.
- SQLAlchemy async setup.
- Alembic.
- Dockerfile and `docker-compose.yml`.
- `.env.example`.
- Ruff configuration.
- Type-check configuration.
- pytest configuration.
- Minimal README with setup and run commands.

### Required environment variables

- `APP_ENV`
- `LOG_LEVEL`
- `DATABASE_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_URL`
- `TELEGRAM_WEBHOOK_SECRET`
- `LLM_PROVIDER`
- `OPENAI_API_KEY`
- `LLM_MODEL`
- `CONTENT_ENCRYPTION_KEY`
- `RAW_CONTENT_RETENTION_DAYS`

### Acceptance criteria

- `docker compose up` starts API, bot, and PostgreSQL.
- Liveness endpoint returns 200.
- Readiness endpoint verifies database access.
- Unit test suite runs.
- Formatter/linter and type checker run from documented commands.

## Milestone 1 — User onboarding and navigation

### Goal

A Telegram user can enter the product, give consent, and navigate the basic menu.

### Deliverables

- `User` database model and migration.
- `/start` handler.
- 18+ confirmation.
- Consent version storage.
- Main menu:
  - Analyze a conversation
  - Previous analyses
  - Credits
  - Privacy and deletion
- Centralized Russian strings.
- Basic rate limiting.
- Analytics interface and no-op implementation.

### Acceptance criteria

- New user is persisted once.
- Repeated `/start` is idempotent.
- User cannot submit an analysis before consent.
- Main menu works after restart.
- Tests cover new and returning users.

## Milestone 2 — Conversation intake and parser

### Goal

Accept pasted or forwarded text and normalize it into a reliable internal representation.

### Deliverables

- FSM for new analysis.
- `Analysis` database model and migration.
- Conversation parser supporting at least:
  - `Name: message`
  - `[timestamp] Name: message`
  - Telegram-like copied multiline text
- Stable message IDs: `m1`, `m2`, etc.
- Participant detection.
- Character/token limit.
- Validation messages for too-short, one-sided, and oversized content.
- Context questions:
  - Which participant are you?
  - What do you want to understand?
  - Optional relationship stage
- Parser fixtures.

### Acceptance criteria

- A valid two-person conversation produces ordered normalized JSON.
- Multiline messages remain attached to the correct participant.
- Empty and one-participant content is rejected.
- Oversized content returns splitting guidance.
- Parser unit tests cover all supported formats and malformed examples.

## Milestone 3 — LLM analysis pipeline

### Goal

Generate a validated structured analysis from normalized messages.

### Deliverables

- Domain Pydantic models matching `PRODUCT_SPEC.md`.
- `LLMClient` protocol.
- Mock LLM client for tests and local demo.
- OpenAI-backed adapter.
- Versioned prompt files.
- Analysis service.
- JSON schema validation.
- One repair retry after invalid output.
- Timeout and retry policy.
- Prompt/model metadata stored on `Analysis`.
- Structured logging with content redaction.

### Acceptance criteria

- Mock client produces a complete valid result.
- Invalid first response is repaired once.
- Two invalid responses mark analysis failed without charging the user.
- Evidence references point only to known message IDs.
- Tests cover success, timeout, invalid JSON, invalid evidence refs, and repair failure.

## Milestone 4 — Report renderer and Telegram delivery

### Goal

Turn structured analysis into a clear Russian-language Telegram report.

### Deliverables

- Report renderer independent of Telegram handlers.
- Telegram-safe chunking for long reports.
- Sections:
  - Summary
  - Observable signals
  - Dynamic direction
  - Observable reciprocity score
  - Hypotheses
  - Unknowns
  - Next actions
  - Suggested replies
- Low/medium/high confidence mapping.
- Buttons:
  - Generate reply options
  - Ask a follow-up
  - Analyze a newer fragment
  - Delete analysis
- Feedback prompt from 1 to 5.

### Acceptance criteria

- Report respects Telegram message length constraints.
- Renderer does not expose raw JSON.
- No section presents hypotheses as facts.
- Analysis history can reopen a completed report.
- Snapshot or unit tests cover representative report variants.

## Milestone 5 — Credits and paywall

### Goal

Create a monetizable flow with correct accounting and local testability.

### Deliverables

- `CreditTransaction` model and migration.
- Ledger-based credit balance.
- Product codes:
  - `analysis_single`
  - `analysis_pack_5`
  - `subscription_monthly`
- Configurable analysis price.
- Free preview logic.
- Atomic spend operation.
- Automatic refund on technical failure.
- Idempotency keys.
- `PaymentProvider` protocol.
- Mock payment provider.
- Checkout UI and payment callback handling.

### Acceptance criteria

- One analysis cannot be charged twice.
- Concurrent requests cannot make balance negative.
- Technical failure creates one refund.
- Repeated payment callback credits the user once.
- End-to-end local test covers preview → mock checkout → paid analysis.

### Production continuation

The deliverables above cover M5A. Production code through M5D is implemented. Production-ready
completion now requires the five live staging acceptance gates and a true release-readiness
snapshot as defined in `docs/milestone-5-roadmap.md` and `docs/release-gates.md`.

## Milestone 6 — Privacy, deletion, retention

### Goal

Make sensitive data handling explicit and testable.

### Deliverables

- Sensitive content encryption abstraction.
- No raw content in logs.
- Delete one analysis.
- Delete all user data.
- Retention cleanup command/job.
- Tombstone or audit approach that preserves financial ledger integrity without retaining conversation text.
- Privacy menu explaining retention and deletion.

### Acceptance criteria

- Deleting an analysis removes raw and normalized content.
- Deleting all data removes all non-required personal content.
- Financial transactions remain internally reconcilable without storing message content.
- Cleanup job deletes expired raw content.
- Tests confirm no raw text is emitted in logs on success or error.

## Milestone 7 — Analytics and admin observability

### Goal

Measure funnel performance and diagnose failures without exposing private content.

### Deliverables

- Analytics provider interface.
- No-op/local provider.
- Events listed in `PRODUCT_SPEC.md`.
- Correlation IDs.
- Basic admin endpoint or command for:
  - analyses by status;
  - completion rate;
  - average model latency;
  - average token/cost estimate;
  - purchase count;
  - error categories.
- Sentry-compatible error boundary or equivalent interface.

### Acceptance criteria

- Main funnel events fire once per transition.
- Analytics properties contain no raw conversation or generated report text.
- Admin metrics can distinguish user validation failures from technical failures.

## Milestone 8 — Deployment readiness

### Goal

Prepare a first production deployment.

### Deliverables

- Webhook mode.
- Startup migration strategy.
- Production Docker image.
- Health checks.
- Graceful shutdown.
- Background job strategy for analysis and retention cleanup.
- Database backup notes.
- Secret management notes.
- Minimal deployment guide for one managed container platform.
- CI workflow for lint, type check, tests, and image build.

### Acceptance criteria

- CI passes on a clean checkout.
- Application can run with webhook mode behind HTTPS.
- Restart does not duplicate active jobs or payments.
- Failed analysis jobs can be retried safely.

## First task for Codex

Start with **Milestone 0 only**.

Before coding:

1. Read `AGENTS.md`, `PRODUCT_SPEC.md`, and this file.
2. Inspect the repository root and avoid changing unrelated B2B catalog files.
3. Place the application inside `ideas/ai-relationship-platform/app/` or another self-contained directory under `ideas/ai-relationship-platform/`.
4. Present a concise implementation plan.
5. Implement the complete Milestone 0 vertical slice.
6. Run checks and report exact commands and results.

Do not implement Telegram business flows, LLM prompts, or payments during Milestone 0 beyond interfaces or placeholders needed for bootstrapping.

## Later product experiments — not MVP commitments

Record these as future hypotheses, not current implementation scope:

1. “Will my ex return?” acquisition funnel leading into evidence-based breakup support.
2. Dedicated “analyze my ex” mode.
3. Screenshot and voice-message ingestion.
4. Timeline comparison across multiple analysis sessions.
5. Relationship diary and event tracking.
6. Interactive romantic stories using the same audience and payment infrastructure.
7. AI oracle or entertainment mode clearly separated from evidence-based analysis.
8. Creator/referral dashboard for Instagram and TikTok traffic partners.
9. Shareable result cards that reveal no private message content.
10. Web app and mobile clients backed by the same domain API.
