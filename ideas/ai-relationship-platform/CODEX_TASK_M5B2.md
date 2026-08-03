# Codex task: Milestone 5B.2 — Production one-time payments

This file is the complete source of truth for the task. Work from the current checkout only. Do not fetch a GitHub issue and do not stop because outbound GitHub API access is unavailable.

## Repository state and stacked workflow

- Repository: `vladimirutkin2mn-lgtm/fabric_bot`
- Project directory: `ideas/ai-relationship-platform`
- Required current branch: `codex/milestone-5b2-one-time-payments`
- Required starting commit: `eb8200f03802d10db95810a79a62598be6df326c`
- This branch is intentionally stacked on PR #16, Milestone 5B.1.
- PR #16 is open and green but is not merged.

Before editing, run:

```bash
git branch --show-current
git rev-parse HEAD
test -f ideas/ai-relationship-platform/CODEX_TASK_M5B2.md
```

Do not create another branch.

While PR #16 remains open, create the M5B.2 pull request against:

`codex/implement-milestone-5b.1-as-per-specifications`

This keeps the new pull request limited to the M5B.2 delta. Do not target `main` while PR #16 is still open. After PR #16 is merged, the existing M5B.2 pull request will be retargeted to `main` separately.

Read this entire task file before editing. Remove `CODEX_TASK_M5B2.md` from the final implementation branch before publishing the pull request.

Do not merge any pull request.

## Goal

Implement production-safe one-time payments for HeartSignal through:

- YooKassa for `RU` market and `RUB`;
- Stripe Checkout for `INTERNATIONAL` market and `EUR` or `USD`.

The implementation must create hosted checkout sessions, receive and authenticate provider notifications, retrieve authoritative provider state, complete a payment exactly once, and grant credits exactly once.

All real provider execution must remain controlled by the M5B.1 feature flags and production fail-closed configuration.

## Scope

This pull request must implement:

1. YooKassa one-time redirect checkout.
2. Stripe hosted Checkout Session in one-time `payment` mode.
3. Stable provider idempotency keys and crash-safe checkout creation.
4. Stripe webhook signature verification using the unmodified raw request body.
5. YooKassa webhook source verification with trusted-proxy handling.
6. Durable webhook inbox insertion and asynchronous processing.
7. Authoritative payment retrieval from each provider before granting credits.
8. Payment reconciliation for uncertain and stale pending states.
9. Exactly-once credit granting under duplicate and concurrent events.
10. A transactional billing outbox for analytics and other non-financial side effects.
11. Minimal user-facing checkout and return-status flow using the existing application architecture.
12. Reversible migration, focused tests, security tests, concurrency tests and documentation.

## Out of scope

Do not implement in this pull request:

- subscriptions or recurring charges;
- saved payment methods;
- Stripe Billing subscription lifecycle;
- YooKassa recurring payments;
- customer-initiated or provider refunds;
- credit refund consumption;
- customer portal;
- disputes or chargebacks;
- two-stage YooKassa capture;
- manual capture in Stripe;
- real production payments during tests;
- real provider credentials committed to the repository.

Subscriptions belong to M5B.3. Refunds, disputes and broader reconciliation belong to M5B.4.

## Provider dependencies and boundaries

Use official maintained provider libraries where they improve security, especially Stripe webhook verification. Keep all vendor objects inside provider-specific modules. No Stripe or YooKassa SDK object may cross the provider-neutral boundary.

Suggested structure:

```text
app/providers/payments/stripe.py
app/providers/payments/yookassa.py
app/providers/payments/stripe_gateway.py
app/providers/payments/yookassa_gateway.py
app/services/checkout_service.py
app/services/webhook_inbox_service.py
app/services/payment_completion_service.py
app/services/payment_reconciliation_service.py
app/services/billing_job_worker.py
app/services/billing_outbox_service.py
app/api/webhooks.py
```

Adapt this to the existing project structure instead of creating duplicate composition roots.

Provider adapters must be dependency-injected. Automated tests must use fake gateways or mocked transports and must not make network calls.

Configure explicit request timeouts. Classify errors into safe typed categories such as:

- validation/configuration error;
- authentication error;
- provider-declared permanent failure;
- retryable provider error;
- transport timeout with unknown provider outcome;
- malformed provider response.

Never log provider secrets, webhook secrets, full webhook bodies, payment-method data or receipt contacts.

## Server-authoritative offer selection

Use the M5B.1 `BillingCatalog` as the sole authority for:

- product code and version;
- credit amount;
- market;
- provider;
- currency;
- amount or Stripe Price reference;
- purchase mode.

Only one-time products are allowed in this pull request:

- `analysis_single`;
- `analysis_pack_5`.

Reject `subscription_monthly` in the one-time checkout service.

Required routing:

```text
RU + RUB -> YooKassa
INTERNATIONAL + EUR -> Stripe
INTERNATIONAL + USD -> Stripe
```

Do not infer market from an IP address. Do not accept provider, amount, credits, price reference or currency conversion from the client.

Persist the immutable M5B.1 commercial snapshot before any provider call.

## Checkout API and domain command

Implement a typed application command equivalent to:

```python
create_one_time_checkout(
    user_id,
    product_code,
    market,
    currency,
    receipt_contact=None,
) -> CheckoutResult
```

The command must:

1. Check `BILLING_ENABLED`, provider flag and kill-switch rules.
2. Resolve a server-owned `BillingOffer`.
3. Reject subscription offers.
4. Lock the user using the existing user-first lock order.
5. Create or reuse one active `PaymentOrder` for the same user/provider/product/market/currency.
6. Store immutable commercial terms before leaving the database transaction.
7. Assign a stable idempotency key derived from the order identity, for example `checkout:create:{order_id}:v1`.
8. Claim checkout creation with a durable lease.
9. Commit before calling the provider.
10. Call the provider outside the database transaction.
11. Persist provider identifiers, hosted URL, provider status and safe request ID in a second transaction.
12. Reuse an existing pending hosted checkout when it is still valid.

The client may never send an arbitrary success URL, cancel URL or return URL. Build all URLs from `PAYMENT_PUBLIC_BASE_URL`.

### Unknown checkout creation outcome

A network timeout after the provider may have created a checkout is not a confirmed failure.

For timeout or ambiguous transport errors:

- do not mark the order permanently failed;
- retain the same stable provider idempotency key;
- record a safe `provider_status='unknown'` or equivalent;
- enqueue a payment reconciliation job;
- allow a retry with the same idempotency key;
- never create a second active internal order for the same commercial purchase.

A later retry must not generate a new Stripe Checkout Session or YooKassa Payment when the provider already accepted the first request.

## YooKassa checkout

Implement one-stage YooKassa payment creation:

- server-to-server API request;
- HTTP Basic Auth from `YOOKASSA_SHOP_ID` and `YOOKASSA_SECRET_KEY`;
- `Idempotence-Key` from the stable order key;
- `capture=true`;
- amount from the immutable snapshot;
- currency `RUB`;
- confirmation type `redirect`;
- return URL from configured public base URL;
- safe description;
- metadata containing the internal order ID and product version, without secrets or sensitive personal data.

Persist:

- YooKassa payment ID as the provider checkout/payment identity as appropriate;
- `confirmation_url` as the hosted checkout URL;
- provider request ID when available;
- current provider status;
- provider test/live indicator when available, without weakening production configuration checks.

Only `succeeded` is a successful one-stage YooKassa payment. `pending` remains pending. `canceled` is terminal failure. `waiting_for_capture` is unexpected for this one-stage integration and must go to safe failure or manual review without granting credits.

### YooKassa receipts

Respect the M5B.1 receipt configuration.

When `YOOKASSA_RECEIPTS_REQUIRED=true`, checkout creation must require a validated customer email or phone suitable for receipt delivery and include a receipt with one server-defined item. Do not use Telegram username as an email. Do not store the plain receipt contact in logs, analytics, metadata or the commercial snapshot.

Use an existing encryption utility for temporary durable storage if one exists. If none exists, add a small isolated encryption abstraction using the configured content-encryption secret, with key separation from conversation content. Store only what is necessary for safe retries. Add tests proving that representations and logs do not expose the contact.

When receipts are not required, omit the receipt object.

## Stripe Checkout

Create a Stripe-hosted Checkout Session:

- `mode='payment'`;
- use the catalog-owned Stripe Price ID for the exact currency and product;
- quantity `1`;
- success and cancel URLs built from the configured public base URL;
- include the internal order ID in safe metadata and `client_reference_id`;
- use the stable idempotency key in request options;
- do not accept inline client-defined amount, currency or Price ID;
- do not enable subscription mode in this pull request.

Persist:

- Checkout Session ID as `provider_checkout_id`;
- hosted session URL;
- PaymentIntent ID once available;
- provider request ID when available;
- current session/payment status;
- expiry timestamp when returned.

Successful return from the Stripe-hosted page is not proof of payment and must never grant credits.

Authoritative completion requires a fetched Checkout Session whose payment state is paid, with matching internal metadata, currency and amount. Support delayed payment methods by processing `checkout.session.async_payment_succeeded`. `checkout.session.completed` with an unpaid state must remain pending.

## HTTP endpoints

Add provider endpoints using the existing FastAPI routing and application composition style:

```text
POST /webhooks/stripe
POST /webhooks/yookassa
```

Add or adapt a safe return-status endpoint/page such as:

```text
GET /payments/return/{checkout_token}
```

The return endpoint may display only internal status and safe identifiers. It must not complete a payment or grant credits based on query parameters.

Do not create a public unauthenticated endpoint that accepts arbitrary `user_id`. User checkout initiation must use the existing trusted Telegram/user context or the existing authenticated application boundary.

## Stripe webhook authentication

Verify Stripe webhook signatures before parsing or persisting an event.

Required inputs:

- exact raw request body bytes;
- `Stripe-Signature` header;
- configured endpoint secret.

Do not let FastAPI, Pydantic or JSON middleware reserialize the body before verification.

Reject invalid signatures with a safe 4xx response and persist nothing.

Use Stripe event ID as the durable provider event ID.

Handle at least:

- `checkout.session.completed`;
- `checkout.session.async_payment_succeeded`;
- `checkout.session.async_payment_failed`;
- `checkout.session.expired`;
- relevant `payment_intent.succeeded` and `payment_intent.payment_failed` events when useful for reconciliation.

Ignore unrelated event types safely with a successful acknowledgement after authentication, without creating financial side effects.

## YooKassa webhook authentication

For YooKassa notifications:

1. Determine the direct peer IP from the ASGI request scope.
2. Trust `Forwarded` or `X-Forwarded-For` only when the direct peer belongs to `YOOKASSA_TRUSTED_PROXY_ALLOWLIST`.
3. Parse the forwarded chain defensively and select the correct original client address.
4. Require the resolved source address to belong to `YOOKASSA_WEBHOOK_IP_ALLOWLIST`.
5. Reject malformed or untrusted forwarding headers.
6. Do not authorize a notification solely because the body claims to be from YooKassa.

For duplicate identity, derive a deterministic event key from provider, event type and provider object ID because YooKassa notifications do not provide a Stripe-like event ID.

Handle at least:

- `payment.succeeded`;
- `payment.canceled`;
- other payment events as non-final pending events where appropriate.

IP verification is not enough to grant credits. The worker must retrieve the current payment from YooKassa and validate it authoritatively.

## Durable webhook inbox

Webhook HTTP handlers must be thin:

1. Authenticate the notification.
2. Extract only safe event identity and object identity.
3. Compute SHA-256 of the exact raw payload for duplicate-mismatch detection.
4. Insert one `ProviderWebhookEvent` using the unique provider/event key.
5. Insert or reuse one `BillingJob(job_type='webhook_processing')` in the same transaction.
6. Return a successful acknowledgement quickly after durable commit.

Do not store raw webhook payloads.

Duplicate delivery with the same event ID and same payload hash must be acknowledged idempotently.

The same provider event ID with a different payload hash must be marked `manual_review`, must not grant credits, and must emit a safe security log without payload contents.

For YooKassa, return HTTP 200 after a valid notification is durably accepted. Invalid source or malformed input must not be accepted as valid.

## Billing job worker

Implement actual claiming and processing for these existing M5B.1 job types:

- `webhook_processing`;
- `payment_reconciliation`.

Use PostgreSQL row locking with `FOR UPDATE SKIP LOCKED`, claim IDs and expiring leases.

Required worker properties:

- multiple worker instances may run concurrently;
- one job has one effective claim at a time;
- expired claims can be recovered;
- attempt counts are incremented durably;
- retryable errors use bounded backoff;
- permanent validation mismatches become `manual_review`;
- completed jobs are idempotent on replay;
- kill switch does not block webhook processing or reconciliation.

No tight polling loop in tests. Make one-iteration worker methods independently testable.

## Authoritative provider retrieval

The worker must not trust amount, currency or paid state from a browser return or unverified body.

### Stripe

Fetch the Checkout Session by ID and retrieve or expand the related PaymentIntent as needed.

Validate:

- live/test mode matches environment expectations;
- session mode is `payment`;
- session is paid for successful completion;
- internal order ID in metadata/client reference matches;
- provider checkout ID matches the order;
- total amount matches the immutable commercial snapshot;
- currency matches;
- PaymentIntent ID is not already owned by another order.

### YooKassa

Fetch the Payment by ID using server credentials.

Validate:

- provider object ID matches the order;
- current status is authoritative;
- `paid` and `succeeded` semantics are consistent;
- amount and currency match the immutable snapshot;
- metadata order ID matches;
- production does not accept a test payment;
- payment is not already owned by another order.

Any mismatch must grant zero credits and transition to a safe terminal or manual-review state with a non-sensitive failure code.

## Exactly-once payment completion

Refactor the existing completion flow so that provider-specific webhook DTOs are not treated as authoritative payment state.

Complete an order in one database transaction using a normalized authoritative payment object.

Required lock order:

```text
User -> PaymentOrder -> ProviderWebhookEvent/BillingJob -> CreditTransaction -> BillingOutboxEvent
```

Document any necessary deviation.

Successful completion must atomically:

- lock the user and order;
- revalidate commercial snapshot against authoritative provider state;
- set the order to completed;
- persist provider payment identity and final provider status;
- create exactly one positive `purchase` credit transaction with idempotency key derived from the order;
- create transactional outbox events for non-financial side effects;
- mark the webhook event/job completed when processing a webhook job.

No network call or analytics call may occur while the financial database transaction is open.

Duplicate webhook delivery, duplicate worker execution, simultaneous Stripe events and reconciliation must produce:

- one completed order;
- one purchase ledger row;
- one effective credit grant;
- no negative or duplicated balance;
- idempotent terminal outcomes.

A canceled or failed payment must never create a purchase ledger row.

## Transactional billing outbox

Fix the existing crash window where analytics state can be committed before or separately from external analytics delivery.

Add a durable append-only outbox model, for example `BillingOutboxEvent`, with:

- ID;
- aggregate type and aggregate ID;
- event type;
- safe JSON payload;
- unique idempotency key;
- pending/claimed/completed/failed/manual_review state;
- attempt count;
- availability and lease fields;
- created/completed timestamps;
- safe last error code.

Create outbox rows in the same transaction as the financial state change.

At minimum support:

- `checkout_started`;
- `purchase_completed`;
- `payment_failed`.

Implement one-iteration outbox delivery worker with leases and retries. External analytics failure must not roll back a completed payment and must not lose the event. Repeated delivery must use an event idempotency key when supported and be safe otherwise.

Remove or stop using boolean emission flags where the outbox makes them obsolete, but preserve backward compatibility through a reversible migration.

## Reconciliation

Implement payment reconciliation for:

- orders left in `creating` after the creation lease;
- provider status `unknown` after ambiguous checkout creation;
- pending orders older than a configurable threshold;
- webhook events stuck in processing after lease expiry;
- failed retryable jobs below retry limit.

Reconciliation must call the authoritative provider fetch endpoint when a provider object ID exists.

When only a stable idempotency key exists after an ambiguous create request, retry provider creation using the same key rather than creating a new order or new key.

Reconciliation outcomes:

- authoritative success -> exactly-once completion;
- authoritative pending -> keep pending and schedule a later check;
- authoritative canceled/expired/failed -> terminal failure, no credits;
- provider object missing while creation outcome is still plausibly unknown -> bounded retry, then manual review;
- amount/currency/metadata mismatch -> manual review, no credits.

## Database migration

Create a new reversible Alembic revision after `20260803_06`. Do not modify M5B.1 migration `20260803_06`.

Add only the fields and tables required for M5B.2. Likely requirements include:

- checkout expiry and last reconciliation timestamps on `payment_orders`;
- a `manual_review` order status or equivalent safe state;
- provider API/live-mode metadata where needed;
- webhook processing lease/timestamps if the existing inbox fields are insufficient;
- transactional billing outbox table;
- indexes for stale pending orders, pending jobs and outbox claims.

Preserve existing M5 data.

Migration must pass:

```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

The isolated `MIGRATION_SCHEMA` regression from M5B.1 must remain green.

## Minimal user experience

Integrate with the existing Telegram and application flow without redesigning the whole bot.

At minimum:

- the user can select a supported one-time product;
- trusted application context supplies the user identity;
- market and currency are explicit server-validated selections;
- checkout creation returns or sends the hosted provider URL;
- repeated taps reuse the active order/URL;
- kill switch or unavailable provider produces a clear safe message;
- YooKassa receipt contact is requested only when required;
- the return/status page says pending, paid or failed based only on internal order state;
- no card or bank details are collected by HeartSignal.

Do not expose internal exceptions, provider secrets or raw provider responses to the user.

## Security requirements

Add tests and implementation for:

- Stripe raw-body signature verification;
- invalid or missing Stripe signature rejected;
- Stripe signed event with mutated body rejected;
- YooKassa direct allowlisted IP accepted;
- YooKassa unallowlisted IP rejected;
- spoofed `X-Forwarded-For` rejected from an untrusted peer;
- trusted proxy chain resolved correctly;
- malformed forwarded chain rejected;
- webhook payload size limit;
- no raw webhook payload persistence;
- no secret/contact leakage in logs or object representations;
- configured HTTPS return and webhook URLs only in production;
- provider live/test mode mismatch rejected;
- client cannot override amount, credits, provider, currency or Price ID;
- browser return cannot complete payment;
- kill switch blocks new checkout but not webhook receipt, processing or reconciliation.

Use constant-time comparison where manual signature comparison is required. Prefer official Stripe verification instead of custom cryptography.

## Required tests

Use real PostgreSQL for concurrency and worker-claim tests. Use fake provider gateways for all provider interactions.

Add focused test modules such as:

```text
tests/test_stripe_provider.py
tests/test_yookassa_provider.py
tests/test_payment_webhooks.py
tests/test_payment_completion_postgres.py
tests/test_checkout_creation_postgres.py
tests/test_billing_jobs_postgres.py
tests/test_billing_outbox_postgres.py
tests/test_payment_reconciliation_postgres.py
tests/test_one_time_payment_routes.py
tests/test_one_time_payment_telegram.py
```

Required scenarios:

### Checkout creation

1. RU/RUB creates YooKassa redirect checkout from immutable snapshot.
2. International EUR/USD creates Stripe hosted Checkout Session using catalog Price ID.
3. Ten concurrent checkout requests create one active internal order and one effective provider checkout.
4. Repeated request returns the same active hosted URL.
5. Provider timeout after accepting create request is recovered with the same idempotency key.
6. Kill switch blocks checkout before provider call.
7. Unsupported product/market/currency is rejected before provider call.
8. Subscription product is rejected in M5B.2.

### Webhooks

9. Valid Stripe signature inserts one durable inbox event and one job.
10. Invalid Stripe signature inserts nothing.
11. Valid YooKassa IP notification inserts one inbox event and one job.
12. Spoofed proxy headers insert nothing.
13. Fifty duplicate webhook deliveries create one inbox row and one effective processing job.
14. Same provider event ID with a different payload hash becomes manual review.
15. HTTP handler returns after durable persistence and does not call analytics or grant credits directly.

### Completion

16. Authoritative YooKassa `succeeded` grants credits once.
17. Authoritative Stripe paid Checkout Session grants credits once.
18. `checkout.session.completed` with unpaid status grants nothing and stays pending.
19. Delayed Stripe async success later grants credits once.
20. YooKassa canceled, Stripe expired or failed PaymentIntent grants nothing.
21. Amount mismatch grants nothing and enters manual review.
22. Currency mismatch grants nothing and enters manual review.
23. Metadata/order mismatch grants nothing and enters manual review.
24. Test-mode provider object in production grants nothing.
25. Ten concurrent duplicate completions create one purchase ledger row.
26. Concurrent webhook processing and reconciliation create one purchase ledger row.
27. Payment identity reused across two orders is rejected.

### Jobs and outbox

28. Multiple workers claim jobs with `SKIP LOCKED` without double processing.
29. Expired job lease is recoverable.
30. Retryable error increments attempts and reschedules with bounded backoff.
31. Permanent mismatch goes to manual review.
32. Purchase completion and outbox insertion are atomic.
33. Analytics outage leaves a pending outbox event and does not lose the completed purchase.
34. Repeated outbox delivery has one effective external event.

### Reconciliation

35. Stale creating order with accepted provider create is recovered.
36. Pending provider success missed by webhook is completed through reconciliation.
37. Pending provider cancellation missed by webhook is closed without credits.
38. Unknown provider result is never treated as confirmed failure solely because of a timeout.

All existing 251 tests from M5B.1 must remain green. Do not skip, weaken or delete financial, concurrency, migration or monotonic-access tests.

## Documentation

Add or update:

```text
docs/one-time-payments.md
docs/payment-webhook-security.md
docs/payment-operations-runbook.md
docs/payment-state-machines.md
```

Document:

- provider routing;
- hosted checkout flows;
- stable idempotency keys;
- crash windows and recovery;
- webhook authentication;
- trusted proxy algorithm;
- durable inbox and job processing;
- authoritative provider retrieval;
- exact-once credit completion;
- billing outbox;
- reconciliation;
- kill-switch behavior;
- receipt handling;
- live/test mode separation;
- operational alerts and manual-review procedure;
- configuration steps that remain disabled by default.

Include Mermaid sequence diagrams for Stripe and YooKassa one-time payments and state diagrams for order, webhook job and outbox processing.

## CI and validation

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

Do not commit while a required command fails.

CI must not contact Stripe or YooKassa. No sandbox provider request is permitted in the default test suite. Optional manual sandbox tests must be explicitly opt-in and excluded from normal CI.

## Delivery

Before publishing:

1. Delete `ideas/ai-relationship-platform/CODEX_TASK_M5B2.md`.
2. Verify the task file is absent from the final diff.
3. Commit the implementation to the current branch `codex/milestone-5b2-one-time-payments`.
4. Push the same branch.
5. Open one pull request titled:

   `Add production one-time payments with YooKassa and Stripe`

6. While PR #16 is open, use base branch:

   `codex/implement-milestone-5b.1-as-per-specifications`

7. Do not create another branch or another pull request.
8. Do not merge.

The pull request description must include:

- base and head branches;
- base and head SHAs;
- migration revision;
- changed-file count;
- exact number of new tests;
- exact total pytest count;
- Stripe checkout and webhook verification results;
- YooKassa checkout and IP/proxy verification results;
- duplicate event and concurrent completion results;
- reconciliation results;
- outbox crash-recovery results;
- Alembic upgrade/downgrade/upgrade result;
- Ruff, mypy and Docker Compose results;
- final GitHub Actions run and conclusion;
- confirmation that no real provider credentials were used;
- confirmation that no provider network call or real payment occurred in CI;
- confirmation that the PR was not merged.

Do not claim that subscriptions or refunds are complete.

Correct completion statement:

> Milestone 5B.2 production one-time payments are implemented and verified for YooKassa and Stripe. Subscription billing, recurring charges and monetary refunds remain disabled and belong to subsequent Milestone 5B pull requests.
