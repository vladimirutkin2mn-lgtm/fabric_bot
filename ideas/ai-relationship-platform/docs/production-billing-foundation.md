# Production billing foundation

Milestone 5B.1 defines durable, provider-neutral billing infrastructure only. All live flags default to off; it creates no checkout endpoint, worker, provider network adapter, renewal, or refund execution.

## Safety and configuration

`BILLING_ENABLED` is the master flag. `BILLING_KILL_SWITCH` prevents new checkouts, renewals, and refunds while webhook ingestion, reconciliation, and billing-state reads remain permitted. Provider, subscriptions, refunds, and YooKassa recurring each have separate flags. Production validation requires HTTPS, enabled-provider credentials, live Stripe keys, a subscription offer when subscriptions are enabled, and rejects mock. Secrets use `SecretStr` and validation errors identify only the unsafe field.

## Catalog and routing

The versioned server catalog is authoritative: `RU/RUB` routes to YooKassa and `INTERNATIONAL/EUR|USD` to Stripe. It owns price references, credit units, provider, product version, purchase mode, and interval; API input must never supply commercial values or infer market from IP. Payment orders preserve an immutable JSON commercial snapshot so catalog changes cannot alter an existing order.

## Persistence

Billing customers map users to provider customers. Subscriptions persist consent, periods, encrypted payment-method storage, and renewal leases; a partial unique index allows one active-like subscription per user/product. Webhook events form a payload-free inbox (only SHA-256 hashes). Refund requests, reservations, and billing jobs are durable state machines. Jobs are schemas only in this milestone.

## Credits and locking

The ledger remains append-only. `purchase_refund` is a distinct future negative entry linked to the purchase, payment order, and refund request; M5B.1 never writes it. Balance is `ledger - active reservations`, clamped at zero. Reserve, spend, consume, and release lock **User first**, then the operation entity, then read ledger/reservations. This stable order serializes spend-versus-reserve races. Reservation creation and terminal operations are idempotent.

## Later stages

M5B.2 will add signed provider ingress and checkout adapters, M5B.3 subscriptions/renewals, and M5B.4 provider refunds and reconciliation. No real credentials, payments, or network calls belong here.
