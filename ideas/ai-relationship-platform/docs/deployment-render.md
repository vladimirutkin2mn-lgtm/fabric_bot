# Production deployment on Render

This runbook describes the first supported production topology for HeartSignal. The repository root contains `render.yaml`, which creates one managed PostgreSQL database, one public API service and two private workers.

## Runtime topology

- `heartsignal-api` receives HTTPS health checks, Telegram updates and payment webhooks. In webhook mode the API owns the aiogram `Bot` and `Dispatcher`; do not deploy the standalone polling bot.
- `heartsignal-billing-worker` processes durable billing jobs, payment reconciliation and the billing outbox. Billing remains disabled until provider credentials and product configuration are complete.
- `heartsignal-maintenance-worker` clears expired encrypted source content and recovers analyses left in `processing` beyond the configured lease.
- `heartsignal-db` is the source of truth for product, billing, deletion, analytics and job state.

The image runs as a non-root user, exposes `/health/live` and `/health/ready`, and handles `SIGTERM` with a bounded graceful-shutdown window.

## First deployment

1. Create a Render Blueprint from the repository and review the resources declared in `render.yaml`.
2. Fill every environment variable marked `sync: false` before the first deploy.
3. Set `TELEGRAM_WEBHOOK_URL` to the final public API URL plus `/telegram/webhook`, for example `https://your-service.onrender.com/telegram/webhook`.
4. Generate a random Telegram webhook secret containing only letters, digits, `_` and `-`; production requires at least 32 characters.
5. Generate and store a strong content-encryption key. Losing this key makes encrypted reports unreadable; exposing it compromises retained private content.
6. Set `PAYMENT_PUBLIC_BASE_URL` to the public HTTPS API origin. Keep `BILLING_ENABLED=false` until YooKassa or Stripe is fully configured and tested.
7. Deploy. The pre-deploy command runs `python -m app.cli.release`, obtains a PostgreSQL advisory lock and upgrades Alembic to `head` before the new API and workers start.
8. Verify `/health/live`, `/health/ready`, one Telegram `/start`, and the admin metrics endpoint if it was explicitly enabled.

## Secret management

Store secrets only in Render environment variables or an external secret manager. Never commit values to `.env`, Blueprint YAML, logs or pull-request descriptions.

At minimum, treat these as secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `CONTENT_ENCRYPTION_KEY`
- `OPENAI_API_KEY`
- `ADMIN_API_TOKEN`
- payment-provider keys and webhook secrets

Rotate a webhook or provider secret by updating the environment first, deploying, and then updating the corresponding provider. Rotate the content-encryption key only through a planned data-reencryption procedure; simply replacing it will make existing ciphertext unreadable.

## Database backups and restore drills

Enable the managed PostgreSQL backup/PITR option appropriate for the selected plan. Keep database access private and restrict any temporary public access by source IP.

Before a risky migration or provider launch:

1. Confirm the latest automated backup completed.
2. Take an additional logical backup with `pg_dump --format=custom` from an authorized environment.
3. Record the application commit SHA and Alembic revision.
4. Restore the backup into a separate database and run `/health/ready`, an analysis read, and a ledger reconciliation check.

Do not treat an untested backup as recoverable. Schedule a restore drill at least quarterly and before major billing/schema changes.

## Deploy, rollback and migrations

The release command is safe to invoke more than once and serializes concurrent deploys through a PostgreSQL advisory lock. Application instances never run migrations during ordinary startup.

For a code rollback, redeploy a previously known-good image or commit. Before rolling back across a schema change, inspect the Alembic migration and its downgrade guards. Privacy and financial migrations may intentionally refuse destructive downgrades when live data exists. Prefer a forward fix when downgrade safety is uncertain.

## Background jobs and restart behavior

Billing jobs and outbox work use durable database state, leases, idempotency keys and stale-claim takeover. A restart can repeat an attempt but must not duplicate a purchase, refund or credit grant.

Analysis execution remains request-scoped in this MVP. The maintenance worker finds analyses whose `processing_started_at` exceeds `ANALYSIS_PROCESSING_STALE_SECONDS` and locks bounded batches with `FOR UPDATE SKIP LOCKED`.

- An unpaid preview or a paid analysis whose spend is still active returns atomically to `draft`. The original interrupted request can no longer commit a terminal result after that state transition.
- A paid analysis whose spend has already been refunded becomes `failed` with `worker_interrupted_refunded`. It is financially closed and is never reopened automatically.

For a known transient failed analysis, requeue it explicitly:

```bash
python -m app.cli.retry_analysis \
  --analysis-id <analysis-uuid> \
  --user-id <user-uuid>
```

The command rejects permanent validation failures, non-failed analyses and analyses whose credit spend was refunded. After a successful requeue, repeat the original preview/full action so the existing entitlement and ledger rules are applied again. For a refunded paid analysis, start a new analysis; the original credit has already been returned. Do not update analysis status manually in SQL.

## Operations checklist

Before enabling traffic:

- CI is green, including image build and migration regression.
- `/health/ready` succeeds against the production database.
- Telegram reports the expected webhook URL and no recent delivery errors.
- `APP_ENV=production`, HTTPS webhook URL and strong secrets pass fail-closed validation.
- only the API owns Telegram webhook delivery; no polling bot is running.
- the billing kill switch is understood and provider webhooks remain reachable even when new checkout is disabled.
- maintenance and billing workers are running exactly once per intended worker replica.
- backup retention and a restore owner are documented.

## Local production-image smoke test

```bash
cp .env.example .env
docker compose build
docker compose up postgres migrate api maintenance-worker
curl --fail http://localhost:8000/health/ready
```

Local polling remains available by leaving `TELEGRAM_WEBHOOK_URL` empty and starting the `bot` service. Production must use the HTTPS webhook path instead.
