# Privacy, deletion and retention operations

Analysis source/context and validated results live in `analysis_private_content` as binary,
versioned AES-256-GCM envelopes. HKDF derives independent source and result keys from
`CONTENT_ENCRYPTION_KEY`; every write uses a fresh nonce. New writes leave legacy plaintext null.

Source expiry is set when source is submitted (30 days by default). Completed encrypted reports do
not automatically expire. Analysis deletion clears all content in every state. Account deletion
removes Telegram/profile identity, analysis content and active checkout secrets while retaining an
identity-free internal UUID.

## Retained financial matrix

Immutable credit transactions are never deleted or rewritten. Provider/order identifiers,
product/version, amount, currency, safe status/error codes and timestamps remain for internal
reconciliation. Receipt contact and checkout URL are cleared. This is a minimization decision, not a
claim about universal statutory retention.

New purchase ledger rows retain `external_payment_provider` together with the raw
`external_payment_id`; uniqueness is enforced on that pair. This preserves provider-native payment
IDs for reconciliation without incorrectly treating identifiers issued by different providers as the
same payment. Pre-migration immutable rows keep a null provider and remain valid without rewriting.

## Rollout and scheduling

1. Run `alembic upgrade head`.
2. Deploy encrypted-writing code.
3. Dry-run and then run `python -m app.cli.backfill_private_content`.
4. Verify count-only queries show zero non-null legacy content columns.
5. Schedule `python -m app.cli.retention_cleanup --batch-size 100` in cron or a managed scheduler.

Both commands use bounded batches and `FOR UPDATE SKIP LOCKED`. Keep keys in a secret manager. V1
supports future format migration, but rotation currently requires controlled re-encryption. Deleted
content and identity cannot be recovered.
