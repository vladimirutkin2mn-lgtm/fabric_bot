# Payment state machines

Financial locking order is `User -> PaymentOrder -> webhook/job -> CreditTransaction ->
outbox`. Provider retrieval happens before that transaction. A unique purchase key and
payment identities make completion exactly once under webhook/reconciliation races.

```mermaid
stateDiagram-v2
 [*] --> creating
 creating --> pending: hosted checkout saved
 creating --> creating: unknown / same-key retry
 creating --> manual_review: retries exhausted
 pending --> completed: authoritative paid
 pending --> failed: authoritative canceled/expired
 pending --> manual_review: validation mismatch
 completed --> completed: duplicate
```

```mermaid
stateDiagram-v2
 [*] --> pending
 pending --> claimed
 claimed --> completed
 claimed --> pending: retry/backoff
 claimed --> manual_review: permanent mismatch
 claimed --> claimed: expired lease recovered
```

```mermaid
stateDiagram-v2
 [*] --> pending
 pending --> claimed
 claimed --> completed: delivered
 claimed --> pending: delivery outage
 claimed --> failed: retry limit
 claimed --> claimed: expired lease recovered
```

Inbox and job creation share a transaction. Workers claim through PostgreSQL
`FOR UPDATE SKIP LOCKED` with expiring leases. Reconciliation covers stale creating,
unknown, old pending, expired claims, and retryable failures. It fetches authoritative
state when an ID exists, or repeats creation with the stable key after ambiguous create.
