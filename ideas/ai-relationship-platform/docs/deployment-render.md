# Production deployment on Render

This runbook describes the first supported production topology for HeartSignal. The repository root contains `render.yaml`, which creates one managed PostgreSQL database, one public API service and three private workers.

## Runtime topology

- `heartsignal-api` receives HTTPS health checks, Telegram updates and payment webhooks. Telegram ingress authenticates the secret header, validates and size-bounds the update, encrypts it into PostgreSQL, and returns `204` without running aiogram handlers.
- `heartsignal-telegram-worker` claims encrypted Telegram updates, decrypts one update in memory, runs the aiogram dispatcher, and erases the payload after completion or terminal failure.
- `heartsignal-billing-worker` processes durable billing jobs, payment reconciliation and the billing outbox. Billing remains disabled until provider credentials and product configuration are complete.
- `heartsignal-maintenance-worker` clears expired encrypted analysis source content and recovers analyses left in `processing` beyond the configured lease.
- `heartsignal-db` is the source of truth for product, billing, deletion, analytics and job state.

The image runs as a non-root user, exposes `/health/live` and `/health/ready`, and handles `SIGTERM` with a bounded graceful-shutdown window.

## First deployment

1. Create a Render Blueprint from the repository and review the resources declared in `render.yaml`.
2. Fill every environment variable marked `sync: false` before the first deploy.
3. Set `TELEGRAM_WEBHOOK_URL` to the final public API URL plus `/telegram/webhook`, for example `https://your-service.onrender.com/telegram/webhook`.
4. Generate a random Telegram webhook secret containing only letters, digits, `_` and `-`; production requires at least 32 characters.
5. Generate and store a strong content-encryption key. Losing this key makes encrypted reports and pending Telegram updates unreadable; exposing it compromises retained private content.
6. Set `PAYMENT_PUBLIC_BASE_URL` to the public HTTPS API origin. Keep `BILLING_ENABLED=false` until YooKassa or Stripe is fully configured and tested.
7. Deploy. The pre-deploy command runs `python -m app.cli.release`, obtains a PostgreSQL advisory lock and upgrades Alembic to `head` before the new API and workers start.
8. Verify `/health/live`, `/health/ready`, one Telegram `/start`, and the admin metrics endpoint if it was explicitly enabled.

## Telegram delivery guarantees

Telegram delivery is handled as an **at-least-once** workflow:

1. The API inserts the update into `telegram_update_inbox` using Telegram `update_id` as the primary deduplication key.
2. The raw JSON payload is encrypted with AES-GCM and a Telegram-specific HKDF purpose before it is committed. Active-update deduplication uses a keyed, purpose-separated fingerprint rather than a plain content hash.
3. Only after that transaction commits does the API return `204` to Telegram.
4. The private worker claims rows with `FOR UPDATE SKIP LOCKED`, a unique claim ID and a bounded lease.
5. A stale worker cannot complete a claim that has already been reclaimed.
6. Successful and permanently failed rows retain only non-content operational metadata; ciphertext, active fingerprint and Telegram user ID are erased.
7. Account deletion also scrubs any pending or claimed updates for that Telegram identity through a database trigger.

A worker crash before terminal commit can cause the same update to run again after lease expiry. Business transitions, payment operations and credit ledger writes must therefore remain idempotent. A crash after an outbound Telegram message but before inbox completion may repeat that message; this is an accepted at-least-once boundary for the first production release.

Run exactly **one Telegram worker replica** for now. The database queue is lease-safe, but the current aiogram FSM storage is process-local and does not yet guarantee per-user ordering across multiple worker replicas. Users can recover durable product state with `/start` after a restart.

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
4. Restore the backup into a separate database and run `/health/ready`, an analysis read, a Telegram inbox claim/complete smoke test, and a ledger reconciliation check.

Do not treat an untested backup as recoverable. Schedule a restore drill at least quarterly and before major billing/schema changes.

## Deploy, rollback and migrations

The release command is safe to invoke more than once and serializes concurrent deploys through a PostgreSQL advisory lock. Application instances never run migrations during ordinary startup.

For a code rollback, redeploy a previously known-good image or commit. Before rolling back across a schema change, inspect the Alembic migration and its downgrade guards. Privacy and financial migrations may intentionally refuse destructive downgrades when live data exists. Prefer a forward fix when downgrade safety is uncertain.

## Background jobs and restart behavior

Billing jobs and outbox work use durable database state, leases, idempotency keys and stale-claim takeover. A restart can repeat an attempt but must not duplicate a purchase, refund or credit grant.

Analysis execution runs inside the durable Telegram worker in this release. The maintenance worker finds analyses whose `processing_started_at` exceeds `ANALYSIS_PROCESSING_STALE_SECONDS` and locks bounded batches with `FOR UPDATE SKIP LOCKED`.

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
- the public API only enqueues Telegram updates; the private Telegram worker owns handler execution.
- exactly one Telegram worker replica is running.
- inbox pending age, claimed lease age, retry exhaustion and terminal failure categories are monitored.
- the billing kill switch is understood and provider webhooks remain reachable even when new checkout is disabled.
- maintenance and billing workers are running exactly once per intended worker replica.
- backup retention and a restore owner are documented.

## Local production-image smoke test

For webhook mode, set a non-empty `TELEGRAM_WEBHOOK_URL` and secret in `.env`, then run:

```bash
cp .env.example .env
docker compose --profile webhook build
docker compose --profile webhook up postgres migrate api telegram-worker maintenance-worker
curl --fail http://localhost:8000/health/ready
```

Local polling remains available by leaving `TELEGRAM_WEBHOOK_URL` empty and starting the `bot` service. Never run the polling bot and webhook Telegram worker against the same bot token at the same time.
