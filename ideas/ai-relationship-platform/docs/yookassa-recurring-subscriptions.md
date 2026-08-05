# YooKassa recurring subscriptions

Milestone 5B.3B.2 adds merchant-managed monthly renewals for the RU/RUB subscription offer.

## Provider model

YooKassa does not create or schedule a separate subscription object for this integration.
HeartSignal owns the schedule and cancellation state:

1. The initial hosted payment is created with `save_payment_method=true`.
2. Credits are granted only after an authoritative `succeeded` payment with `paid=true`.
3. The returned `payment_method.id` is encrypted with the billing content key before it is stored.
4. At the paid period boundary, a durable `subscription_renewal` job creates a new payment using the encrypted saved method and a stable idempotency key.
5. A provider webhook and the renewal job may race; both normalize to the same subscription period and the ledger grants credits exactly once.
6. Cancel and resume change the local renewal schedule. They never remove credits from an already paid period.

Plaintext payment method identifiers, raw webhook payloads, receipt contacts, card details, and provider credentials must not be stored in subscription records or logs.

## Required configuration

```text
BILLING_ENABLED=true
YOOKASSA_ENABLED=true
SUBSCRIPTIONS_ENABLED=true
YOOKASSA_RECURRING_ENABLED=true
YOOKASSA_SHOP_ID=...
YOOKASSA_SECRET_KEY=...
PRODUCT_SUBSCRIPTION_MONTHLY_PRICE_MINOR=...
PRODUCT_SUBSCRIPTION_MONTHLY_CREDITS=...
PAYMENT_PUBLIC_BASE_URL=https://...
CONTENT_ENCRYPTION_KEY=...
YOOKASSA_WEBHOOK_IP_ALLOWLIST=...
```

When receipts are required, configure `YOOKASSA_RECEIPTS_REQUIRED=true` and a valid
`YOOKASSA_RECEIPT_EMAIL`. The contact is sent to YooKassa for the receipt request and is
not persisted in the subscription row.

The billing kill switch blocks new initial checkouts and renewals. Already received
webhooks remain processable so authoritative provider state can still be reconciled.

## Durable identities

- Synthetic provider subscription ID: `yookassa:<initial_order_id>`
- Initial checkout: `subscription:checkout:<order_id>:v1`
- Renewal job: `subscription:renewal:<subscription_id>:<period_boundary>`
- Renewal provider payment: `subscription:renewal:<subscription_id>:<period_boundary>:payment:v1`
- Period and credit identities remain the provider-neutral keys defined by M5B.3A.

Never generate a new provider idempotency key when retrying an unknown outcome.

## Webhooks

Accept only the configured YooKassa source networks and the existing supported events:

- `payment.succeeded`
- `payment.canceled`
- `payment.waiting_for_capture`

At ingress, events whose provider metadata contains `billing_mode=subscription` are
classified for the subscription worker. Only the event identity, object identity, type,
and payload hash are stored; the raw body is not persisted.

## Sandbox acceptance checklist

1. Create a RU/RUB monthly subscription and confirm the hosted page displays recurring-payment consent.
2. Complete the initial payment in test mode.
3. Verify one completed initial order, one paid subscription period, one purchase ledger entry, and an encrypted payment-method envelope.
4. Replay the same webhook and reconciliation job; balances and periods must not change.
5. Move a test subscription to a due boundary and run two workers concurrently; only one provider payment identity and one credit grant may result.
6. Simulate an unknown HTTP outcome, then deliver the success webhook before the retry; the retry must reconcile the existing payment rather than charge the next period.
7. Simulate a canceled renewal; no credits are granted and the subscription becomes `past_due`.
8. Disable renewal in Telegram and verify no due job is created after the paid boundary.
9. Resume before the boundary and verify renewal is scheduled again.
10. Delete the user and verify new renewals cannot be created while retained financial records contain no plaintext payment method.

Automated tests use fake transports only. Real provider test-mode acceptance is a separate deployment gate.
