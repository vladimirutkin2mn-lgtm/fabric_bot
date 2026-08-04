# HeartSignal Milestone 7 — Analytics and admin observability

## Repository and branch

Repository:

`vladimirutkin2mn-lgtm/fabric_bot`

Work only on:

`codex/milestone-7-analytics-admin-observability`

The branch starts from the Milestone 6 squash merge on `main`:

`491185ab566808d3bf54ab9afeea709fe4989004`

Do not implement Milestone 8 deployment workers or webhook transport.

Do not implement subscriptions, provider refunds, OCR, voice or new LLM features.

Do not merge the final pull request without an explicit user instruction.

Delete this task file before the final pull request is marked ready.

## Source of truth

Read and follow, in order:

1. `AGENTS.md`
2. `PRODUCT_SPEC.md`
3. `TASKS.md`
4. existing services, repositories, API/bot composition, migrations and tests

## Goal

Implement the complete Milestone 7 vertical slice:

- product funnel transitions emit privacy-safe analytics exactly once per durable transition;
- local PostgreSQL analytics storage is available behind the existing provider interface;
- analytics can be disabled with a no-op provider;
- HTTP and Telegram work carry a safe correlation ID into logs, analytics and error reporting;
- an authenticated admin endpoint exposes operational and product aggregates without private content;
- an error-reporting interface is compatible with a future Sentry adapter but has no required external dependency;
- user validation failures are distinguishable from technical failures;
- analytics and error reporting never contain conversations, generated reports, prompts, receipt contacts, provider secrets or Telegram identity.

## Required analytics events

At minimum support the events from `PRODUCT_SPEC.md`:

- `bot_started`
- `onboarding_completed`
- `analysis_started`
- `conversation_submitted`
- `conversation_rejected`
- `preview_viewed`
- `paywall_viewed`
- `checkout_started`
- `purchase_completed`
- `analysis_processing_started`
- `analysis_completed`
- `analysis_failed`
- `reply_suggestions_requested`
- `followup_requested`
- `analysis_deleted`
- `all_data_deleted`

Existing safe internal lifecycle events may remain.

## Analytics persistence

Add a dedicated `analytics_events` table after revision `20260804_08`.

Minimum fields:

- UUID primary key;
- event name;
- pseudonymous internal subject ID, nullable;
- safe JSONB properties;
- unique idempotency key;
- correlation ID, nullable;
- timestamp.

Do not add a foreign key to `users`; account deletion must not break aggregate history.

Use `INSERT ... ON CONFLICT DO NOTHING` for idempotent writes.

Stable transition events must derive deterministic keys from the durable entity:

- user transition — internal User UUID;
- analysis transition — Analysis UUID;
- payment transition — PaymentOrder UUID;
- account deletion — tombstone User UUID.

Attempt/action events may use correlation ID.

Project safe billing outbox transitions (`checkout_started`, `purchase_completed`, optionally `payment_failed`) transactionally into analytics so payment events cannot be lost between the billing commit and analytics delivery.

## Privacy-safe contract

Create a strict event/property registry.

Requirements:

- unknown event names are rejected by the durable provider;
- only allow-listed property keys are stored for each event;
- values are short scalar strings;
- raw conversation, participant names, user goal, report text, prompt text, model output, Telegram ID/username/name, receipt contact, checkout URL, webhook payload/signature, provider secret, encryption key and ciphertext are forbidden;
- exception text must not contain rejected property values;
- no analytics object repr exposes properties.

Analytics failures must never roll back an already committed business transition.

## Correlation IDs

Add a context-variable boundary shared by API, bot, logging, analytics and error reporting.

HTTP:

- accept a valid bounded `X-Correlation-ID` or generate a random ID;
- return it in the response header;
- set/reset context around the complete request;
- never trust arbitrary unbounded header content.

Telegram:

- derive a non-sensitive ID from the update ID, or generate one when unavailable;
- set/reset context around the complete update;
- do not include Telegram user/chat identity.

Logging format must include `correlation_id` and safely fall back to `-` outside a request/update.

## Error reporting

Add a typed `ErrorReporter` protocol and no-op/logging implementation.

The interface may later be adapted to Sentry, but this milestone must not require a Sentry SDK or network call.

Capture unexpected HTTP and Telegram exceptions with only allow-listed context:

- surface;
- operation or route template;
- safe exception class;
- correlation ID.

Never send exception messages, request bodies, Telegram messages, private properties or secrets.

## Admin observability

Add an authenticated endpoint, for example:

`GET /admin/metrics`

Use a constant-time comparison against `ADMIN_API_TOKEN`.

The endpoint must not be enabled in production with an empty or placeholder token.

Return typed JSON including at least:

- analyses by status;
- completed and failed terminal counts;
- completion rate;
- average model latency;
- average input/output/total tokens;
- average analysis cost units;
- purchase transaction count and purchased-credit total;
- required funnel-event counts;
- analysis failure codes;
- user validation failure counts from conversation rejection reasons;
- technical failure total;
- pending/claimed/manual-review billing job counts;
- pending/manual-review billing outbox counts.

No row-level private data may be returned.

## Composition

- keep `AnalyticsClient` provider-neutral;
- add a PostgreSQL implementation and retain `NoOpAnalyticsClient`;
- centralize provider creation for both API and bot so they use the same configuration;
- remove the current production validation that unconditionally forbids enabled analytics once the local provider is configured;
- keep external analytics vendors out of scope.

## Tests

Add unit and PostgreSQL tests covering:

1. allow-listed events persist;
2. duplicate durable transition events persist once;
3. different entities persist separately;
4. action events use correlation IDs;
5. unknown events and forbidden keys fail without echoing values;
6. billing outbox projection is transactional and idempotent;
7. HTTP correlation ID generation, validation, propagation and response header;
8. Telegram correlation middleware does not use Telegram identity;
9. logging includes correlation ID;
10. error reporting receives only safe context and exception class;
11. admin authentication rejects missing/wrong tokens;
12. admin aggregates are correct on representative PostgreSQL fixtures;
13. validation and technical failures are separated;
14. sentinel private values do not appear in analytics rows, admin JSON, logs, reporter calls or exception strings;
15. existing product flows remain green.

## Documentation

Update:

- `.env.example`;
- README with analytics/admin settings and a safe curl example;
- an observability document describing event semantics, privacy rules and metrics.

## Final validation

Run:

```bash
ruff format app tests migrations
ruff format --check app tests migrations
ruff check app tests migrations
mypy app tests
python -m compileall -q app tests migrations
alembic upgrade head
alembic downgrade -1
alembic upgrade head
pytest
cp .env.example .env
docker compose config --quiet
git diff --check
```

Before final delivery:

- delete `CODEX_TASK_M7.md`;
- open one pull request targeting `main`;
- confirm the task file is absent from the main-to-head diff;
- report exact test count and GitHub Actions result;
- do not merge.