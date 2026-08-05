# Provider monetary refunds

This runbook covers M5B.4 refunds for completed Stripe and YooKassa purchases.
Refunds are disabled by default and must remain disabled until the provider-specific staging
checklists below pass.

## Safety model

1. The user selects a completed purchase within `BILLING_REFUND_WINDOW_DAYS`.
2. The application locks the user and purchase, verifies the original purchase ledger row and
   reserves unused whole credit units.
3. A durable `refund_reconciliation` job owns all provider I/O. The Telegram handler never calls a
   provider directly.
4. Provider creation uses the refund request's stable idempotency key.
5. Unknown or pending provider outcomes retain the credit reservation and are reconciled later.
6. Authoritative failure releases the reservation exactly once.
7. Authoritative success consumes the reservation and appends one negative `purchase_refund`
   ledger transaction tied to the original purchase and provider refund identity.
8. Commercial, identity or live-mode mismatches enter manual review without releasing credits.

A refund of a subscription payment does not cancel future renewal. The customer must separately
turn off auto-renewal through subscription management.

## Eligibility policy

Automatic refund eligibility requires all of the following:

- billing and refunds are enabled and the kill switch is off;
- the user is active;
- the payment order completed within the configured policy window;
- the provider payment identity and original purchase transaction exist;
- enough unused, unreserved credits remain in the user's global balance;
- the requested credits do not exceed the unrefunded credits from the purchase;
- partial refunds are supported for the provider and receipt configuration.

Stripe supports full and partial refunds. YooKassa partial refunds are disabled by this application
when receipt data would be required, because an itemized refund receipt is not stored in the
billing database. Full YooKassa refunds remain supported through the provider's original-payment
receipt data.

## Configuration

```dotenv
BILLING_ENABLED=true
REFUNDS_ENABLED=false
BILLING_REFUND_WINDOW_DAYS=14
```

Enable `REFUNDS_ENABLED` only after deployment verification, both provider sandbox checklists and
manual-review operations are ready. The billing kill switch blocks new refund requests while
allowing already-created requests to reconcile.

## Stripe staging checklist

- Use Stripe test credentials and a staging webhook endpoint.
- Complete a one-time test purchase and verify one positive purchase ledger entry.
- Request a full refund and verify:
  - one credit reservation;
  - one Stripe refund with the expected PaymentIntent and amount;
  - one negative `purchase_refund` ledger entry;
  - the reservation is consumed;
  - replay/reconciliation does not create a second refund or ledger row.
- Repeat with a partial refund and verify exact minor-unit allocation.
- Force a pending or transport-unknown outcome and verify the same idempotency key is reused.
- Force an authoritative failure and verify the reservation is released once.
- Verify live-mode, amount, currency and payment-identity mismatches enter manual review.

## YooKassa staging checklist

- Use YooKassa test-shop credentials and the staging billing worker.
- Complete a RUB test purchase and verify one positive purchase ledger entry.
- Request a full refund and verify:
  - `POST /v3/refunds` uses the original `payment_id`;
  - the `Idempotence-Key` is stable and no longer than 64 characters;
  - one negative `purchase_refund` ledger entry is created after authoritative success;
  - replay/reconciliation remains exactly once.
- When receipt mode is disabled, test a supported partial refund and minor-unit allocation.
- When receipt mode is required, verify the Telegram flow offers only a full refund.
- Force `pending`, `canceled`, timeout and malformed-commercial-state scenarios.
- Verify pending/unknown outcomes remain reserved, canceled releases the reservation and mismatches
  enter manual review.

## Manual review

Manual review must compare all of the following before any operator action:

- internal refund request and original payment order;
- provider payment and provider refund identities;
- amount, currency and live/test mode;
- reservation status and existing `purchase_refund` ledger row;
- provider dashboard state.

Never create a second provider refund with a new idempotency key merely because a request timed
out. First retrieve or repeat the original request using the stored identity and key.

## Metrics and alerts

Monitor at least:

- refund requests by provider/status;
- age of `provider_pending` requests;
- active reserved credits;
- manual-review count and reason;
- provider failure rate;
- succeeded refunds without a ledger row, and ledger rows without succeeded refunds;
- reconciliation attempts and exhausted retries.
