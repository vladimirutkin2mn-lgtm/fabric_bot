# Analytics and admin observability

Milestone 7 adds product and operational visibility without storing conversations, generated
reports, Telegram identity or payment secrets in analytics.

## Configuration

```dotenv
ANALYTICS_BACKEND=noop
ERROR_REPORTING_BACKEND=logging
ADMIN_METRICS_ENABLED=false
ADMIN_API_TOKEN=
```

`ANALYTICS_BACKEND=noop` disables durable product analytics. `postgres` writes validated events
to `analytics_events`. The PostgreSQL implementation is local to HeartSignal and does not call an
external analytics vendor.

`ERROR_REPORTING_BACKEND=logging` reports only the exception class, safe operation name, surface
and correlation ID. Exception messages and request/update content are not reported. `noop`
disables the reporter.

The admin endpoint is hidden unless `ADMIN_METRICS_ENABLED=true`. In production the token must be
at least 32 characters and must not be a known placeholder.

A safe local request:

```bash
curl --fail \
  -H 'X-Admin-Token: replace-with-local-admin-token' \
  -H 'X-Correlation-ID: local-admin-check-1' \
  http://localhost:8000/admin/metrics
```

Do not put an admin token in a URL, log message, analytics property or committed file.

## Correlation IDs

HTTP accepts `X-Correlation-ID` only when it is a bounded ASCII identifier. Invalid or oversized
values are replaced with a random opaque ID. The selected ID is returned in the response header.

Telegram updates use `tg-update-<update_id>`. Telegram user ID, chat ID, username and message text
are not part of the correlation ID.

Application logs include `correlation_id`. Code running outside an HTTP request or Telegram update
uses `-`.

## Event contract

The durable provider rejects unknown event names and properties outside the per-event allow-list.
Errors are deliberately generic and do not echo rejected values.

Durable transition events use stable idempotency identities:

- user lifecycle: internal User UUID;
- analysis lifecycle: Analysis UUID;
- checkout/payment lifecycle: PaymentOrder UUID;
- all-data deletion: tombstone User UUID.

Action events use the active correlation ID, so a retried handler in the same update/request is
suppressed while a later independent action is counted separately.

Required funnel events:

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

Billing outbox rows for checkout, purchase and payment failure are projected into analytics by a
PostgreSQL trigger in the same transaction. A rolled-back billing transaction therefore cannot
leave a committed analytics event. The trigger selects a fixed safe subset of payload fields.

## Forbidden analytics and error-reporting data

Never add any of the following to analytics properties, correlation IDs, admin responses or error
reporting context:

- raw or normalized conversation messages;
- participant names or the user's question/goal;
- generated report or reply text;
- prompts or model output;
- Telegram ID, username, first name or chat identity;
- receipt email/phone;
- checkout URL;
- webhook body, signature or secret;
- provider credentials;
- encryption keys or ciphertext.

New event names or properties require an explicit contract change and privacy tests.

## Admin metrics

`GET /admin/metrics` returns aggregate-only JSON:

- analyses grouped by status;
- completed/failed counts and terminal completion rate;
- average latency, input/output/total tokens and cost units;
- purchase transaction count and purchased-credit total;
- required funnel-event counts;
- conversation rejection reasons;
- analysis technical failure codes;
- billing job and billing outbox statuses.

Conversation rejections are user-validation failures. Failed analysis rows are technical failures.
They remain separate so input quality is not confused with system reliability.

The endpoint never returns row-level IDs, timestamps, Telegram identity or private content.

## Operational checks

Apply and verify the migration chain:

```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

Run the privacy and aggregate acceptance tests with PostgreSQL:

```bash
pytest tests/test_observability.py \
  tests/test_observability_settings.py \
  tests/test_analytics_postgres.py \
  tests/test_admin_observability_postgres.py
```
