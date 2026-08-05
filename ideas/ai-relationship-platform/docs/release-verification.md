# HeartSignal release verification

Run this gate after every staging or production deployment and before enabling traffic or billing.
It verifies the deployed control plane without sending a valid Telegram update and without reading or
writing user content.

## Prerequisites

Run the command from an environment containing the same deployment configuration as the release:

- `APP_ENV=staging` or `APP_ENV=production`;
- `TELEGRAM_BOT_TOKEN`;
- `TELEGRAM_WEBHOOK_URL`;
- `TELEGRAM_WEBHOOK_SECRET`;
- the remaining required HeartSignal settings, including `DATABASE_URL` and
  `CONTENT_ENCRYPTION_KEY`.

The command does not print token values, webhook secrets, Telegram error text, database URLs, or
HTTP exception details.

## Command

```bash
python -m app.cli.verify_deployment
```

Optional bounds:

```bash
python -m app.cli.verify_deployment \
  --timeout-seconds 10 \
  --max-pending-updates 100 \
  --recent-error-seconds 900
```

For machine-readable output:

```bash
python -m app.cli.verify_deployment --json
```

Exit codes:

- `0`: every check passed;
- `1`: deployment configuration loaded, but one or more remote checks failed;
- `2`: local deployment-verification configuration is invalid.

## Checks

The verifier requires all of the following:

1. `GET /health/live` returns a valid liveness response.
2. `GET /health/ready` confirms database connectivity and that `alembic_version` exactly matches
   the migration head packaged with the running image.
3. `POST /telegram/webhook` with a deliberately wrong secret returns `401`. This proves that the
   public route is reachable and still fails closed without adding an update to the durable inbox.
4. Telegram `getWebhookInfo` reports the exact configured webhook URL.
5. Telegram either omits `allowed_updates` or includes both `message` and `callback_query`.
6. Telegram's pending update count does not exceed the configured bound.
7. Telegram has not reported a delivery error inside the configured recent-error window.

The API readiness endpoint now fails with `503` when the database is reachable but the schema is
missing or behind the image. A plain `SELECT 1` is not sufficient for release readiness.

## Failure handling

- `api_liveness`: inspect the API process, container start command, port and platform routing.
- `api_readiness`: confirm the pre-deploy migration command completed and the API points to the
  intended database. Do not manually edit `alembic_version`.
- `telegram_webhook_authentication`: confirm the deployed route is `/telegram/webhook` and that no
  proxy or fallback route converts an unauthorized request into success.
- `telegram_webhook_configuration`: restart or redeploy the API so startup registration runs, then
  verify the configured public URL.
- `telegram_update_backlog`: inspect Telegram worker health, queue claim age, database capacity and
  repeated handler failures before increasing replicas.
- `telegram_delivery_errors`: inspect platform ingress and Telegram webhook delivery. The verifier
  intentionally suppresses provider error text; use authorized operational tooling for details.

## Manual checks that remain required

The automated verifier does not impersonate a user or enqueue a valid Telegram update. After it
passes:

1. Send `/start` from a dedicated staging or operator account.
2. Begin an intake, restart a Telegram worker, and confirm the FSM continues from PostgreSQL.
3. Confirm a later update for the same account does not overtake the earlier update during the
   restart test.
4. When billing is being enabled, run the provider-specific checkout, webhook, reconciliation and
   refund acceptance tests separately.

Record the deployed commit SHA, Alembic revision, verification output and operator performing the
manual smoke test in the release log.
