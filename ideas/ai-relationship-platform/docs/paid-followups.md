# Paid follow-up question

A completed paid full report includes exactly one contextual follow-up question.

## Correctness model

- Eligibility is tied to an owned `completed` analysis with `report_access=full`, a positive
  paid cost, and the original full-access ledger transaction.
- Reservation locks the analysis first and creates at most one claim-fenced entitlement row.
- Provider I/O happens outside the database transaction.
- A live lease returns `processing`; an expired lease can be reclaimed with a new claim ID.
- Only the current claim can complete or release the entitlement.
- Success stores the encrypted question and answer and consumes the entitlement exactly once.
- Technical failure clears private question/answer content and returns the entitlement to
  `available`.
- Replayed callbacks return the stored answer without a second LLM call.

## Privacy and safety

The follow-up prompt receives only the validated structured report and a bounded question. It
never receives the normalized source conversation. Question and answer history are encrypted
with separate purpose-derived keys. Analytics and logs contain only identifiers, status,
prompt version, attempt counts, and safe failure categories.

The answer can reference only existing structured report sections. It cannot weaken a high-risk
safety signal from the primary report. Soft-deleting the analysis removes the encrypted follow-up
row through a database trigger.

## Operations

A reserved row with an expired lease is safe to reclaim. A completed row is immutable through the
public service boundary. Corrupted encrypted history is not retried automatically; it is surfaced
as support-required. Migration `20260805_15` refuses downgrade while any entitlement state exists.
