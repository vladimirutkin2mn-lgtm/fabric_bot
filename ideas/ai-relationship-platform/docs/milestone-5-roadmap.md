# Milestone 5 continuation roadmap

The original `TASKS.md` Milestone 5 covered the credit ledger, free preview, mock checkout and
paywall. Production billing was later split into smaller stages. M5A, M5A.1, M5B.1 and M5B.2
are complete; the milestone is not fully complete until the stages below are delivered.

## Completed stages

- [x] **M5A — credits, preview and mock payments**
  - append-only integer credit ledger;
  - atomic spend and technical analysis refund;
  - one-time preview entitlement;
  - server-authoritative products;
  - idempotent mock checkout and payment completion.
- [x] **M5A.1 — monotonic report access**
  - report access can move `none -> preview -> full` and never downgrade from `full`.
- [x] **M5B.1 — production billing foundation**
  - fail-closed flags and kill switch;
  - billing catalog and deterministic market/provider routing;
  - durable customers, subscriptions, refund requests, reservations and jobs;
  - provider-neutral interfaces and PostgreSQL concurrency invariants.
- [x] **M5B.2 — production one-time payments**
  - YooKassa RUB and Stripe EUR/USD hosted one-time checkout;
  - authenticated webhook inbox;
  - billing worker, reconciliation and transactional outbox;
  - exactly-once credit granting;
  - Telegram market/currency and receipt-contact flow.

## M5B.3 — subscriptions and renewals

### Goal

Sell and operate `subscription_monthly` without duplicate subscriptions, renewals or credit
grants, while allowing a user to stop future renewals safely.

### Deliverables

- Subscription-specific provider boundary separated from one-time payment adapters.
- Stripe subscription Checkout and authoritative subscription/invoice retrieval.
- YooKassa recurring flow using a saved payment method only when explicitly enabled.
- Durable billing-customer creation and provider-customer identity checks.
- Explicit, versioned recurring-payment consent stored on every subscription.
- Subscription lifecycle:
  - `incomplete`;
  - `active`;
  - `past_due`;
  - `cancel_at_period_end`;
  - `canceled`;
  - `unpaid`;
  - `paused`.
- Initial subscription payment and one credit grant per paid billing period.
- Renewal scheduler and lease-based `subscription_renewal` jobs.
- Stable period idempotency key and exactly-once renewal credit grant.
- Grace-period handling and bounded retry/backoff.
- Webhook ingestion for Stripe subscription/invoice events and YooKassa recurring payments.
- Reconciliation for lost webhook, unknown provider outcome and expired worker lease.
- Telegram UI for:
  - purchasing a subscription;
  - viewing status and current period end;
  - disabling renewal at period end;
  - restoring renewal before the period ends where the provider supports it.
- Outbox notifications for activation, renewal success/failure and cancellation.
- Operations runbook and provider sandbox acceptance checklist.

### Financial and concurrency invariants

- At most one active-like subscription exists per user/product.
- One provider billing period grants credits exactly once.
- Webhook, scheduled renewal and reconciliation races cannot duplicate a charge or grant.
- A stale worker cannot complete or overwrite a reclaimed renewal.
- Cancellation never removes credits already purchased for the current paid period.
- The billing kill switch blocks new subscription creation and renewal requests but does not
  block webhook receipt, state reconciliation or already-settled credit granting.
- No raw provider payload, receipt contact, payment method or Telegram identity enters ledger,
  analytics or logs.

### Acceptance criteria

- Concurrent subscription creation produces one active-like subscription and one provider
  checkout identity.
- Ten concurrent completions for the same initial period create one purchase transaction.
- Webhook versus reconciliation races pass repeatedly with one state transition and grant.
- Ten workers attempting the same renewal produce one provider request owner.
- Replayed invoice/payment events do not duplicate credits.
- Failed renewal enters `past_due`, observes grace period and eventually becomes `unpaid` or
  recovers to `active` from authoritative provider state.
- Cancel-at-period-end is idempotent and preserves current-period access.
- Migration upgrade, downgrade and upgrade pass on PostgreSQL.
- Ruff, strict mypy, full pytest, Compose validation and production image build pass.

## M5B.4 — provider monetary refunds

### Goal

Return real money safely while preventing the associated purchased credits from being spent or
removed twice.

### Deliverables

- Explicit refund eligibility policy and operator/manual-review boundary.
- Full and supported partial refund calculation based on unused whole credit units.
- Credit reservation before the provider refund request.
- Stripe and YooKassa refund creation with stable idempotency keys.
- Authoritative refund retrieval and `refund_reconciliation` jobs.
- Exactly-once negative `purchase_refund` ledger entry after provider success.
- Reservation release after authoritative failure and reservation consumption after success.
- Telegram refund request/status flow and admin/manual-review operations.
- Refund metrics, outbox notifications and operations runbook.

### Acceptance criteria

- Spend-versus-refund reservation races cannot make available balance negative.
- Duplicate provider callbacks or reconciliation cannot produce two monetary or ledger refunds.
- Unknown provider outcomes remain reserved and recover through reconciliation.
- Provider failure releases credits exactly once.
- Successful refund removes only the corresponding unused purchased credits.

## M5C — one paid follow-up question

### Goal

Honor the product promise that a full paid report includes one contextual follow-up question.

### Deliverables

- Durable follow-up entitlement tied to an owned full-access analysis.
- Atomic reserve, consume and release transitions.
- Versioned follow-up prompt using the structured report and bounded user question, not the raw
  conversation unless explicitly required and still retained.
- One repair retry and the same evidence/safety boundaries as the primary analysis.
- Telegram question intake, answer delivery and stored history.
- Technical failure releases the entitlement; successful completion consumes it exactly once.

### Acceptance criteria

- Concurrent requests cannot consume the included follow-up more than once.
- Replayed callbacks reopen the stored answer without another LLM call.
- Failure does not permanently consume the entitlement.
- Deleted or preview-only analyses cannot use a follow-up.

## Release sequence

1. Complete and merge M5B.3.
2. Complete and merge M5B.4.
3. Complete and merge M5C.
4. Deploy staging with provider sandbox credentials.
5. Run documented Telegram, Stripe and YooKassa acceptance tests.
6. Enable limited production traffic only after reconciliation and manual-review paths are
   exercised.
