# Billing state machines

Dashed arrows are future milestones; M5B.1 supplies schemas and invariants only.

## PaymentOrder
```mermaid
stateDiagram-v2
  creating --> pending
  pending --> completed: verified payment
  creating --> failed
  pending --> failed
  pending --> cancelled
```

## Subscription
```mermaid
stateDiagram-v2
  incomplete --> active
  active --> past_due: renewal fails
  active --> cancel_at_period_end
  cancel_at_period_end --> canceled
  past_due --> active: payment recovers
  past_due --> unpaid
  active --> paused
  paused --> active
```
All transitions above are future execution behavior.

## ProviderWebhookEvent
```mermaid
stateDiagram-v2
  pending --> processing
  processing --> completed
  processing --> failed
  failed --> pending: retry
  processing --> manual_review
```

## RefundRequest
```mermaid
stateDiagram-v2
  requested --> credits_reserved
  credits_reserved --> provider_pending
  provider_pending --> succeeded
  provider_pending --> failed
  provider_pending --> manual_review
```
Provider transitions and the matching negative ledger entry are future M5B.4 behavior.

## CreditReservation
```mermaid
stateDiagram-v2
  active --> consumed: refund succeeds
  active --> released: refund abandoned/fails
```

## BillingJob
```mermaid
stateDiagram-v2
  pending --> claimed
  claimed --> completed
  claimed --> failed
  failed --> pending: retry available
  claimed --> manual_review
```
Worker-driven transitions are future work; lease and availability fields make them durable.
