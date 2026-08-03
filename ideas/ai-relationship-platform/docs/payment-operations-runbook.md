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
