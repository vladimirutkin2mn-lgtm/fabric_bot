# Staging release gates

Milestone 5 live-provider acceptance is recorded through an authenticated, append-only control
plane. The control plane does **not** execute provider calls and does not turn CI or fake-provider
tests into live acceptance evidence.

## Gates

A limited-production snapshot requires the latest result for all five gates to be `passed`:

- `stripe_subscription_sandbox`
- `yookassa_subscription_sandbox`
- `stripe_refund_sandbox`
- `yookassa_refund_sandbox`
- `openai_followup_staging`

Each pass must represent the actual staging procedure documented in the corresponding provider
runbook. Record `failed` when a live scenario exposes a problem; a later append-only result can
supersede it after the problem is fixed and the procedure is repeated.

## Release identity

Set these variables on the staging deployment:

```dotenv
RELEASE_CODE_SHA=<exact deployed git commit>
RELEASE_CHECKLIST_VERSION=m5-live-v1
```

An attestation is valid only for the exact tuple:

- staging environment;
- deployed code SHA;
- current Alembic revision;
- checklist version.

A new deployment, schema migration or checklist version makes the previous pass `stale`.
Production and local environments cannot create attestations.

## Authentication

The endpoints reuse the existing admin boundary:

```dotenv
ADMIN_METRICS_ENABLED=true
ADMIN_API_TOKEN=<high-entropy secret>
```

Send the token in `X-Admin-Token`. The API never returns provider credentials or secret values.

## Read readiness

```bash
curl -sS \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  https://staging.example.com/admin/release-readiness
```

`ready_for_limited_production` is true only when every current gate passed, provider configuration
is complete and no financial blocker is present.

## Record a live result

```bash
curl -sS -X POST \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  https://staging.example.com/admin/release-gates/stripe_subscription_sandbox \
  -d '{"status":"passed","evidence_ref":"staging/stripe-subscription/run-2026-08-05"}'
```

`evidence_ref` is an opaque, non-secret reference to an external test run, ticket or deployment
record. It must not contain access tokens, customer data, payment details, query strings or raw
provider payloads.

A `passed` result is rejected when the relevant staging configuration is incomplete. Examples
include live Stripe credentials instead of test credentials, a missing webhook secret, disabled
subscriptions/refunds, missing YooKassa webhook allowlist or an unconfigured OpenAI model.

## Financial blockers

Readiness closes again when the database contains release-unsafe state, including:

- billing jobs or outbox events in `failed` or `manual_review`;
- payment orders or refunds in `manual_review`;
- a successful refund without its `purchase_refund` ledger entry;
- a refund ledger entry without an authoritative successful refund;
- a refund reservation inconsistent with the terminal refund state.

Resolve and reconcile these states before accepting traffic. Do not bypass the gate by inserting,
updating or deleting rows manually: attestations are append-only at the PostgreSQL level, and the
migration refuses downgrade while audit history exists.

## Live acceptance boundary

A gate may be marked passed only after the real staging scenario is complete:

- Stripe and YooKassa subscription flows follow their provider-specific sandbox checklists;
- refund gates cover full/partial, pending, failure, replay and reconciliation scenarios;
- the OpenAI gate exercises the paid follow-up flow using the configured staging model and checks
  structured-report-only prompting, safety and retry behavior.

Automated tests, mock providers and local development evidence are insufficient for these gates.
