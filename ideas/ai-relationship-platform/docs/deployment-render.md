# Production deployment on Render

This runbook describes the first supported production topology for HeartSignal. The repository root contains `render.yaml`, which creates one managed PostgreSQL database, one public API service and three private workers.

## Runtime topology

- `heartsignal-api` receives HTTPS health checks, Telegram updates and payment webhooks. Telegram ingress authenticates the secret header, validates and size-bounds the update, encrypts it into PostgreSQL, and returns `204` without running aiogram handlers.
- `heartsignal-telegram-worker` claims encrypted Telegram updates, decrypts one update in memory, runs the aiogram dispatcher, and erases the payload after completion or terminal failure. aiogram FSM state is stored durably in PostgreSQL and event handling for one FSM key is serialized with PostgreSQL advisory locks.
- `heartsignal-billing-worker` processes durable billing jobs, payment reconciliation and the billing outbox. Billing remains disabled until provider credentials and product configuration are complete.
- `heartsignal-maintenance-worker` clears expired encrypted analysis source content and recovers analyses left in `processing` beyond the configured lease.
- `heartsignal-db` is the source of truth for product, billing, deletion, analytics, Telegram inbox and FSM state.

The image runs as a non-root user, exposes `/health/live` and `/health/ready`, and handles `SIGTERM` with a bounded graceful-shutdown window.

## First deployment

1. Create a Render Blueprint from the repository and review the resources declared in `render.yaml`.
2. Fill every environment variable marked `sync: false` before the first deploy.
3. Set `TELEGRAM_WEBHOOK_URL` to the final public API URL plus `/telegram/webhook`, for example `https://your-service.onrender.com/telegram/webhook`.
4. Generate a random Telegram webhook secret containing only letters, digits, `_` and `-`; production requires at least 32 characters.
5. Generate and store a strong content-encryption key. Losing this key makes encrypted reports, pending Telegram updates and encrypted FSM data unreadable; exposing it compromises retained private content.
6. Set `PAYMENT_PUBLIC_BASE_URL` to the public HTTPS API origin. Keep `BILLING_ENABLED=false` until YooKassa or Stripe is fully configured and tested.
7. Deploy. The pre-deploy command runs `python -m app.cli.release`, obtains a PostgreSQL advisory lock and upgrades Alembic to `head` before the new API and workers start.
8. Verify `/health/live`, `/health/ready`, one Telegram `/start`, an interrupted intake resumed after a worker restart, and the admin metrics endpoint if it was explicitly enabled.

## Telegram delivery guarantees

Telegram delivery is handled as an **at-least-once** workflow:

1. The API inserts the update into `telegram_update_inbox` using Telegram `update_id` as the primary deduplication key.
2. The raw JSON payload is encrypted with AES-GCM and a Telegram-specific HKDF purpose before it is committed. Active-update deduplication uses a keyed, purpose-separated fingerprint rather than a plain content hash.
3. Only after that transaction commits does the API return `204` to Telegram.
4. Private workers claim rows with `FOR UPDATE SKIP LOCKED`, a unique claim ID and a bounded lease.
5. A worker cannot claim a later active update for the same Telegram user while an earlier update is pending or claimed. Different users can be processed concurrently.
6. aiogram FSM state is persisted in `telegram_fsm_state`; FSM data is encrypted with a separate HKDF purpose. PostgreSQL advisory locks serialize the same FSM key across worker processes.
7. A stale worker cannot complete a claim that has already been reclaimed.
8. Successful and permanently failed inbox rows retain only non-content operational metadata; ciphertext, active fingerprint and Telegram user ID are erased.
9. Account deletion scrubs pending or claimed inbox rows and deletes FSM state for that Telegram identity through database triggers.

A worker crash before terminal commit can cause the same update to run again after lease expiry. Business transitions, payment operations and credit ledger writes must therefore remain idempotent. A crash after an outbound Telegram message but before inbox completion may repeat that message; this remains an accepted at-least-once boundary.

Multiple Telegram worker replicas are supported. Begin with one replica, then scale based on queue age and throughput. Same-user updates remain ordered, while different users can run concurrently. Scaling increases PostgreSQL connections and advisory-lock waiters, so verify pool capacity and database headroom before increasing replicas.

## Secret management

Store secrets only in Render environment variables or an external secret manager. Never commit values to `.env`, Blueprint YAML, logs or pull-request descriptions.

At minimum, treat these as secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `CONTENT_ENCRYPTION_KEY`
- `OPENAI_API_KEY`
- `ADMIN_API_TOKEN`
- payment-provider keys and webhook secrets

Rotate a webhook or provider secret by updating the environment first, deploying, and then updating the corresponding provider. Rotate the content-encryption key only through a planned data-reencryption procedure; simply replacing it will make existing ciphertext unreadable, including active FSM data.

## Database backups and restore drills

Enable the managed PostgreSQL backup/PITR option appropriate for the selected plan. Keep database access private and restrict any temporary public access by source IP.

Before a risky migration or provider launch:

1. Confirm the latest automated backup completed.
2. Take an additional logical backup with `pg_dump --format=custom` from an authorized environment.
3. Record the application commit SHA and Alembic revision.
4. Restore the backup into a separate database and run `/health/ready`, an analysis read, a Telegram inbox claim/complete smoke test, an FSM state/data round trip, and a ledger reconciliation check.

Do not treat an untested backup as recoverable. Schedule a restore drill at least quarterly and before major billing/schema changes.

## Deploy, rollback and migrations

The release command is safe to invoke more than once and serializes concurrent deploys through a PostgreSQL advisory lock. Application instances never run migrations during ordinary startup.

For a code rollback, redeploy a previously known-good image or commit. Before rolling back across a schema change, inspect the Alembic migration and its downgrade guards. The FSM migration refuses downgrade while live FSM rows exist because dropping them would silently lose active user flows. Privacy and financial migrations may also refuse destructive downgrades when live data exists. Prefer a forward fix when downgrade safety is uncertain.

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
- the public API only enqueues Telegram updates; private Telegram workers own handler execution.
- an intake flow survives a Telegram worker restart without requiring `/start`.
- same-user updates remain ordered under a two-worker smoke test.
- inbox pending age, claimed lease age, retry exhaustion and terminal failure categories are monitored.
- PostgreSQL connection capacity is sufficient for API, Telegram, billing and maintenance replicas.
- the billing kill switch is understood and provider webhooks remain reachable even when new checkout is disabled.
- backup retention and a restore owner are documented.

## Local production-image smoke test

For webhook mode, set a non-empty `TELEGRAM_WEBHOOK_URL` and secret in `.env`, then run:

```bash
cp .env.example .env
docker compose --profile webhook build
docker compose --profile webhook up postgres migrate api telegram-worker maintenance-worker
curl --fail http://localhost:8000/health/ready
```

Local polling remains available by leaving `TELEGRAM_WEBHOOK_URL` empty and starting the `bot` service. Local polling uses the same durable PostgreSQL FSM storage. Never run the polling bot and webhook Telegram workers against the same bot token at the same time.
