# Stripe subscription operations

This document covers M5B.3B.1 only. YooKassa saved-payment-method renewals are a separate
financial flow and remain M5B.3B.2.

## Runtime contract

Stripe Checkout creates a provider subscription in `mode=subscription`. HeartSignal sends only
server-owned commercial metadata: internal user/order IDs, product/version, market/currency,
expected amount, credits, Price ID and recurring-consent version. Conversation content,
Telegram identity, receipt contacts and payment method details are never sent as metadata.

The application accepts these Stripe events:

- `checkout.session.completed`;
- `invoice.paid`;
- `invoice.payment_failed`;
- `customer.subscription.updated`;
- `customer.subscription.deleted`.

The HTTP endpoint authenticates the Stripe signature and stores only event ID/type, provider
object ID and a payload hash. A private billing worker retrieves authoritative objects from
Stripe before changing financial state.

## Exactly-once period accounting

Every paid Stripe invoice is normalized into one provider period and passed to
`SubscriptionLifecycleService`. The period is unique by subscription and exact UTC boundaries,
while Stripe invoice and payment identities are also unique. One paid period can therefore
produce at most:

- one completed `payment_order`;
- one positive `purchase` ledger transaction;
- one subscription-period outbox event.

Webhook replay, scheduled reconciliation and lease takeover may repeat provider reads, but they
cannot duplicate a credit grant. A job is marked completed only by the current unexpired claim.

## Configuration

Enable production Stripe subscriptions only with all regular Stripe production settings plus:

```dotenv
SUBSCRIPTIONS_ENABLED=true
STRIPE_PRICE_SUBSCRIPTION_MONTHLY_EUR=price_...
STRIPE_AMOUNT_SUBSCRIPTION_MONTHLY_EUR_MINOR=990
STRIPE_PRICE_SUBSCRIPTION_MONTHLY_USD=price_...
STRIPE_AMOUNT_SUBSCRIPTION_MONTHLY_USD_MINOR=1090
BILLING_CONSENT_VERSION=billing-v1
SUBSCRIPTION_GRACE_PERIOD_DAYS=3
```

Every configured Price must have its exact expected amount in minor units. A missing half of a
Price/Amount pair fails configuration validation. Test Stripe credentials remain forbidden in
production by the existing settings guard.

The global billing kill switch blocks new checkout and user-requested renewal changes. It does
not block authenticated webhook receipt, reconciliation or application of a payment that Stripe
has already settled.

## User lifecycle

- A successful paid invoice creates or activates the subscription and grants that period's
  credits.
- `invoice.payment_failed` records `past_due` without granting credits.
- A later paid invoice recovers the same period exactly once.
- Telegram cancellation sets `cancel_at_period_end`; already purchased credits remain intact.
- Telegram resume clears scheduled cancellation when Stripe still permits it.
- Scheduled reconciliation reads Stripe near each stored period boundary.
- After the configured grace period, unresolved `past_due` subscriptions become `unpaid`.

## Sandbox acceptance checklist

Use a dedicated Stripe test account in staging only. Automated CI does not call Stripe.

1. Configure one monthly EUR test Price and its exact expected amount.
2. Register the staging webhook endpoint and all five event types listed above.
3. Start a subscription from Telegram and verify Checkout displays the expected monthly amount.
4. Complete payment with a Stripe test card.
5. Confirm one active subscription, one paid period, one completed order and one purchase ledger
   row.
6. Replay `invoice.paid` and confirm no additional credits are granted.
7. Trigger reconciliation while replaying the event and confirm the same invariant.
8. Disable renewal in Telegram and verify Stripe and HeartSignal both show cancellation at period
   end while current credits remain.
9. Resume before period end and verify both systems return to active renewal.
10. Use a failing renewal card to produce `past_due`; verify no new credits are granted.
11. Recover the invoice and verify one grant for that period.
12. Allow a sandbox subscription to pass the grace boundary and verify terminal state handling.
13. Stop a worker after provider retrieval but before job completion; restart and verify the
    lifecycle remains exactly once.
14. Review billing jobs, webhook inbox and outbox for `manual_review` or exhausted retries.

Do not enable production subscriptions until this checklist passes and M5B.3B.2 plus the
YooKassa sandbox checklist are also complete.
