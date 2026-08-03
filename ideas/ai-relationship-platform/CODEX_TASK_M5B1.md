# Codex task: Milestone 5B.1 — Production billing foundation

This file is the complete source of truth for the task. Do not fetch GitHub issue #15 and do not stop because outbound GitHub access is unavailable. Work from the current checkout only.

## Repository state

- Repository: `vladimirutkin2mn-lgtm/fabric_bot`
- Project directory: `ideas/ai-relationship-platform`
- Required branch: `codex/milestone-5b1-production-billing-foundation`
- Base commit before this task file: `5d7c481d3e0a9e7ee060600522560cfe1bbf60fb`
- PR #13 and PR #14 are already merged into `main`.

Do not create another branch. Do not merge the resulting PR.

## Goal

Implement the production billing foundation required before adding real YooKassa and Stripe API calls.

This PR must add:

1. Production billing configuration and fail-closed validation.
2. Server-authoritative product catalog and market/provider routing.
3. Provider-neutral typed interfaces.
4. Billing database models and a reversible Alembic migration.
5. Credit reservations and available-balance calculation.
6. Durable webhook inbox and billing-job schemas.
7. PostgreSQL concurrency tests.
8. Architecture and state-machine documentation.

All live billing flags must default to disabled.

## Strict scope

Do **not** implement in this PR:

- real YooKassa or Stripe API calls;
- live checkout creation;
- webhook HTTP endpoints or signature verification;
- subscription renewals;
- provider refunds;
- reconciliation network calls;
- Telegram billing UX;
- live credentials or network calls.

The mock provider may remain for tests/local development but must be rejected in production.

## Configuration

Add typed settings equivalent to:

- `ENVIRONMENT`: local/test/production
- `BILLING_ENABLED`
- `BILLING_KILL_SWITCH`
- `YOOKASSA_ENABLED`
- `STRIPE_ENABLED`
- `SUBSCRIPTIONS_ENABLED`
- `REFUNDS_ENABLED`
- `YOOKASSA_RECURRING_ENABLED`
- `PAYMENT_PUBLIC_BASE_URL`
- YooKassa credentials, receipt settings, IP allowlists and trusted proxies
- Stripe secret, webhook secret, portal URL and configured EUR/USD Price IDs
- billing worker lease/retry/reconciliation settings
- subscription grace period and consent version

Production startup must fail when an enabled feature has incomplete or unsafe configuration, including:

- non-HTTPS public URL;
- missing enabled-provider credentials;
- recognizable Stripe test key in production;
- mock provider selected in production;
- recurring YooKassa enabled without YooKassa;
- refunds enabled while billing is disabled;
- subscriptions enabled without a configured subscription offer.

Secrets must not appear in logs, validation errors or object representations.

`BILLING_KILL_SWITCH=true` must block future new checkout, renewal and refund operations, but must not conceptually block webhook receipt, reconciliation or reading existing billing state. Add typed helper methods and tests for these rules.

## Product catalog and routing

Server-authoritative markets:

- `RU` → YooKassa → RUB
- `INTERNATIONAL` → Stripe → EUR or USD

Initial products:

- `analysis_single`
- `analysis_pack_5`
- `subscription_monthly`

Each offer must define product version, purchase mode, credits, market, provider, currency, price reference and billing interval where applicable.

Do not infer market from IP. Reject client-controlled prices, credits or provider identifiers.

Add a typed resolver equivalent to:

```python
resolve_product_offer(product_code, market, currency) -> BillingOffer
```

## Provider interfaces

Create or extend `app/providers/payments/` with typed enums, DTOs and a provider protocol or ABC for future operations:

- one-time checkout;
- subscription checkout;
- fetch payment/subscription/refund;
- cancel subscription;
- recurring payment;
- create refund.

Include typed concepts equivalent to:

- `PaymentProvider`: mock, yookassa, stripe
- `BillingMarket`: RU, INTERNATIONAL
- `PaymentMode`: one_time, subscription_initial, subscription_renewal
- normalized payment/refund statuses
- checkout, payment, subscription, refund and verified-webhook result DTOs

No production adapter may make a network call in this PR.

The provider router must reject `mock` in production.

## Database

Create a new reversible migration. Do not edit deployed migration `20260803_05`.

### BillingCustomer

Add fields equivalent to:

- id
- user_id
- provider
- provider_customer_id
- created_at
- updated_at

Unique constraints:

- `(user_id, provider)`
- `(provider, provider_customer_id)`

### PaymentOrder extensions

Add production fields such as:

- mode
- market
- product_version
- billing_period
- provider_invoice_id
- subscription_id
- provider_status
- idempotency_key
- provider_request_id
- failure_code
- immutable commercial snapshot JSON

The commercial snapshot must contain product code/version, credits, amount, currency, provider, market and billing period. Existing orders must never be recalculated from a later catalog version.

Preserve all existing fields and behavior.

### Subscription

Statuses:

- incomplete
- active
- past_due
- cancel_at_period_end
- canceled
- unpaid
- paused

Include provider/customer references, encrypted payment-method slot, periods, cancellation fields, durable consent, renewal claim/lease fields and last order reference.

Add a PostgreSQL partial unique index preventing multiple active-like subscriptions per user/product. Active-like statuses include at least incomplete, active, past_due, cancel_at_period_end and paused.

### ProviderWebhookEvent

Add a durable inbox with:

- provider
- provider_event_id
- event_type
- provider_object_id
- payload_hash
- processing status
- attempt count
- timestamps
- safe last error code

Unique `(provider, provider_event_id)`. Do not persist raw provider payloads.

### RefundRequest

Add durable request state with:

- user and payment-order references
- provider and provider-refund reference
- requested/credits_reserved/provider_pending/succeeded/failed/manual_review statuses
- amount, currency and whole credit units
- reason
- idempotency key
- provider request ID
- safe failure code
- timestamps

No provider call yet.

### CreditReservation

One reservation per refund request. Statuses: active, consumed, released. Credit units must be positive.

### BillingJob

Add durable job state for:

- webhook processing
- subscription renewal
- payment reconciliation
- refund reconciliation

Include provider/object identity, unique idempotency key, pending/claimed/completed/failed/manual_review status, attempts, availability, claim and lease fields, safe error code and timestamps.

Do not implement worker execution in this PR.

## Ledger and balances

Keep `credit_transactions` append-only.

Add a distinct negative transaction type `purchase_refund`, with references for original purchase, payment order and refund request. Do not reuse the existing positive technical analysis refund.

Do not create a `purchase_refund` row in this PR; provider refund completion belongs to M5B.4.

Expose:

- ledger balance;
- active reservations;
- available balance = ledger balance − active reservations.

Available balance must never be negative. Existing spend operations must not consume actively reserved credits.

Add a focused credit reservation service with idempotent operations:

- reserve for refund;
- consume reservation;
- release reservation;
- retrieve balance.

Use existing user-row serialization and a documented consistent lock order.

## Required PostgreSQL tests

1. Ten concurrent attempts reserve all five available credits → exactly one succeeds.
2. Ten reserve calls for the same refund request → one reservation row.
3. Spend-vs-reserve race with exactly five credits, repeated at least 25 times → only one side succeeds and available balance never becomes negative.
4. Ten concurrent release calls → one effective release.
5. Ten concurrent consume calls → one effective consume.
6. Production configuration rejection matrix.
7. RU/RUB routes only to YooKassa; international EUR/USD routes only to Stripe.
8. Mock provider rejected in production.
9. Migration upgrade → downgrade → upgrade.
10. All existing M5 financial and monotonic-access regressions remain green.

Use real PostgreSQL sessions for concurrency tests. Do not use live provider APIs.

Prefer focused test modules such as:

- `tests/test_billing_config.py`
- `tests/test_billing_catalog.py`
- `tests/test_payment_provider_router.py`
- `tests/test_billing_models_postgres.py`
- `tests/test_credit_reservations_postgres.py`
- `tests/test_billing_migrations.py`

## Documentation

Add:

- `docs/production-billing-foundation.md`
- `docs/payment-state-machines.md`

Document scope, flags, kill switch, routing, entities, lock ordering, immutable snapshots, reservation behavior and later M5B stages. Include Mermaid state diagrams for PaymentOrder, Subscription, ProviderWebhookEvent, RefundRequest, CreditReservation and BillingJob. Clearly mark future transitions.

## Validation

Run from `ideas/ai-relationship-platform`:

```bash
ruff format app tests migrations
ruff format --check app tests migrations
ruff check app tests migrations
mypy app tests
python -m compileall -q app tests migrations
alembic upgrade head
alembic downgrade -1
alembic upgrade head
pytest
cp .env.example .env
docker compose config --quiet
git diff --check
```

Do not commit or push while a required validation command fails.

If outbound GitHub network access is unavailable after implementation, still complete all local code and validation that the environment supports, commit the work locally, and use the environment's built-in PR publishing mechanism. Do not abandon the implementation merely because `curl`, `gh`, or `git fetch` cannot access GitHub.

## Delivery

Work only on the current branch:

`codex/milestone-5b1-production-billing-foundation`

Open one PR targeting `main` with title:

`Add production billing foundation for Milestone 5`

The PR description must include base/head SHAs, migration revision, tables changed, exact tests and CI results, and explicit confirmation that no real credentials, network calls or payments were used.

Do not merge the PR.

Correct completion statement:

> Milestone 5B.1 production billing foundation is implemented and verified. Live provider payment execution remains disabled and belongs to subsequent Milestone 5B pull requests.
