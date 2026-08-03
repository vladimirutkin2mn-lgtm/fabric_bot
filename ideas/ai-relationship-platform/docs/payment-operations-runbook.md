# Payment operations runbook

Keep `BILLING_ENABLED`, `YOOKASSA_ENABLED`, and `STRIPE_ENABLED` false until HTTPS URLs,
live credentials, webhook secrets, provider prices, and IP/proxy CIDRs are independently
verified. Production rejects HTTP public URLs, mock/test Stripe credentials, and
provider test objects. No real credentials belong in source control.

The kill switch blocks new checkout but deliberately does not block webhook ingestion,
workers, or reconciliation. Alert on `manual_review`, exhausted jobs, stale creating or
pending orders, outbox age, payload-digest conflicts, and live-mode mismatches.

For manual review, compare the internal immutable snapshot to the provider dashboard,
without copying customer/payment data into logs. Do not grant credits manually until
identity, amount, currency, mode, metadata, and live mode match. Requeue transient jobs
with their existing idempotency key. Outbox analytics failures never reverse payment.
Subscriptions, recurring charges, refunds, disputes, and manual capture remain disabled.

## Deployment topology

Run the API, Telegram bot, and billing worker as separate processes sharing PostgreSQL.
The worker command is:

```bash
python -m app.workers.billing
```

The `billing-worker` Compose service runs leased webhook/reconciliation jobs, periodically
sweeps stale `creating` and `pending` orders, and delivers transactional outbox events.
It polls at most once per second while idle and handles SIGTERM/SIGINT gracefully. The
checkout kill switch is intentionally not consulted by this recovery process.

## Analytics outbox policy

`ANALYTICS_ENABLED=false` is explicit: the billing worker intentionally discards
non-financial analytics events through a logging sink and records the discard without
customer data. Delivery properties include the outbox event ID and idempotency key for
future at-least-once sinks. Production startup fails closed if analytics is enabled while
no delivery client is configured; it never silently treats a no-op client as delivery.
