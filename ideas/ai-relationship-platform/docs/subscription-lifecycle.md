# Subscription lifecycle foundation — M5B.3A

M5B.3 is split into two delivery stages so provider-specific network behavior cannot weaken the
financial invariants already proven for one-time payments.

- **M5B.3A** — durable subscription periods, lifecycle transitions, scheduler handoff and
  exactly-once credit accounting.
- **M5B.3B** — Stripe subscription Checkout/invoices, YooKassa saved-payment recurring calls,
  provider cancellation/resume, webhook mapping and Telegram subscription UX.

This document describes M5B.3A.

## Core invariant

A provider subscription can have many billing periods, but each canonical UTC period can create
at most:

- one `subscription_periods` row;
- one completed `payment_orders` row;
- one positive `credit_transactions` purchase row;
- one paid-period outbox event.

The canonical period key is derived from the authoritative UTC period start and end, not from a
client callback or mutable catalog value. Provider invoice and payment identifiers have separate
unique constraints to detect cross-period or cross-user identity reuse.

## Transaction boundary

`SubscriptionLifecycleService.apply_paid_period` accepts only provider-verified facts. Under one
PostgreSQL transaction it:

1. locks `User`;
2. validates or creates the provider `BillingCustomer`;
3. locks or creates the active-like `Subscription`;
4. locks or creates the canonical `SubscriptionPeriod`;
5. validates or creates the completed `PaymentOrder`;
6. validates or creates the append-only purchase transaction;
7. marks the period paid and advances the subscription period;
8. inserts a transactional outbox event.

Duplicate webhook and reconciliation executions therefore return `already_applied` without a
second grant. Conflicting provider identities fail closed as a state mismatch.

## Past-due and recovery

An authoritative unpaid invoice creates or updates a `past_due` period without a payment order or
credit transaction. A later authoritative paid result for the same canonical period upgrades that
row to `paid` and grants credits once.

After the configured grace period, `finalize_terminal_states` moves an unresolved `past_due`
subscription to `unpaid`. This transition never removes credits purchased for earlier periods.

## Cancellation

M5B.3A records only provider-confirmed cancellation and resume facts:

- `record_cancel_at_period_end` preserves all current-period credits and stores the effective end;
- `record_resumed` restores `active` only before that end;
- `finalize_terminal_states` moves an elapsed `cancel_at_period_end` subscription to `canceled`.

The provider API call itself belongs to M5B.3B. Local state must not claim cancellation before the
provider confirms it.

## Renewal scheduler handoff

`enqueue_due_renewals` scans active or past-due subscriptions near their period boundary and
inserts one idempotent `subscription_renewal` billing job:

```text
subscription:renewal:<subscription_id>:<period_end_utc>
```

PostgreSQL conflict handling and row leases allow multiple scheduler replicas without duplicate
jobs. M5B.3A does not wire those jobs into the existing one-time worker because provider-managed
Stripe renewals and merchant-initiated YooKassa recurring charges require different authoritative
operations. M5B.3B will add the dedicated job processor before the scheduler is enabled in a
runtime process.

## Safety boundaries

- No provider payload is persisted.
- No card or saved-payment-method value enters the period, order, ledger, outbox or analytics.
- Commercial values originate from the server catalog and authoritative provider retrieval.
- Cancellation does not revoke already purchased credits.
- The billing kill switch and provider feature flags remain the runtime authority in M5B.3B.
- This stage performs no Stripe or YooKassa subscription network call.

## Required next stage — M5B.3B

1. Add a subscription-specific gateway protocol and adapters.
2. Create initial subscription checkout with explicit recurring consent.
3. Map Stripe invoice/subscription events and YooKassa recurring results into the lifecycle
   service.
4. Add a dedicated renewal job processor and reconciliation.
5. Add provider-confirmed cancel/resume operations.
6. Add Telegram purchase, status and cancellation UX.
7. Exercise provider sandbox acceptance tests before enabling subscriptions.
