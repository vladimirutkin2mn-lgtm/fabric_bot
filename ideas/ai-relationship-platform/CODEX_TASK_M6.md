# HeartSignal Milestone 6 — Privacy, deletion and retention

## Repository and branch

Repository:

`vladimirutkin2mn-lgtm/fabric_bot`

Work only on the existing branch:

`codex/milestone-6-privacy-deletion-retention`

The branch was created from `main` at:

`7e35e56583c8030995eaf8aa04206eb14d8989e3`

Do not create another branch.

Do not create a replacement pull request.

Do not merge.

## Required reading

Before changing code, read completely:

1. `AGENTS.md`
2. `PRODUCT_SPEC.md`
3. `TASKS.md`
4. the current models, repositories, services, Telegram handlers, migrations and tests
5. the Milestone 5B.2 payment and worker documentation

The source of truth is `PRODUCT_SPEC.md`, then `TASKS.md`, then this implementation specification.

## Goal

Implement Milestone 6 as a complete privacy vertical slice:

- new sensitive analysis content is encrypted at rest;
- a user can immediately delete one analysis;
- a user can delete all non-required personal data;
- financial records remain internally reconcilable without conversation or report content;
- expired source content is deleted automatically by an idempotent cleanup command/job;
- raw conversation text and generated private report text never appear in logs or analytics;
- deletion and retention remain correct under concurrent analysis, payment and cleanup activity.

## Current baseline and risks

The current implementation stores sensitive fields directly in plaintext database columns on `analyses`, including:

- `normalized_conversation_json`;
- `participants_json`;
- `user_participant_label`;
- `user_goal`;
- `relationship_stage`;
- `result_json`.

Single-analysis deletion currently handles only completed analyses and clears some columns.

The privacy menu is still a placeholder.

`users` contains Telegram identity fields and is referenced with `RESTRICT` by the immutable credit ledger and billing tables. Therefore all-data deletion must use a correctly designed tombstone/anonymization strategy rather than deleting financial rows or breaking foreign keys.

## Non-goals

Do not implement in this pull request:

- subscriptions or recurring payment execution;
- provider monetary refunds;
- OCR, screenshots or voice ingestion;
- a new analytics vendor;
- data export/download;
- automatic training or model-improvement use of user content;
- legal-policy claims about mandatory retention periods;
- provider network calls during deletion or retention;
- deletion of immutable credit ledger entries.

No real Telegram, Stripe, YooKassa or OpenAI network call may occur in automated tests.

---

# 1. Authenticated encryption abstraction

Add a reusable, typed sensitive-content encryption boundary.

Use an established authenticated-encryption implementation from a maintained cryptography library. Do not invent a new XOR, stream-cipher or unauthenticated encryption format.

A suitable implementation is AES-GCM or Fernet from the `cryptography` package.

Requirements:

- derive purpose-specific keys from `CONTENT_ENCRYPTION_KEY`;
- use a random nonce for every encryption operation;
- include a versioned envelope so future key/format migration is possible;
- authenticate ciphertext and reject tampering;
- serialize JSON canonically as UTF-8;
- return typed errors for malformed envelope, unknown version, wrong key and corrupted ciphertext;
- never include the key, plaintext or ciphertext in `repr`, exception text or logs;
- encrypt identical plaintext to different ciphertexts;
- support separate purposes for:
  - analysis source content;
  - analysis result content;
- fail production startup when the encryption key is absent or obviously placeholder/unsafe;
- add the cryptography package as a direct pinned-compatible project dependency.

Suggested boundary:

```python
class SensitiveContentCipher(Protocol):
    def encrypt_json(self, purpose: ContentPurpose, value: object) -> bytes: ...
    def decrypt_json(self, purpose: ContentPurpose, value: bytes) -> object: ...
```

Keep payment receipt-contact compatibility intact. Do not make existing pending YooKassa orders undecryptable. Refactor receipt encryption only if backward compatibility is explicitly preserved and tested.

---

# 2. Private analysis-content storage

Add a dedicated encrypted storage model instead of continuing to write private content into plaintext `analyses` columns.

Recommended model:

`AnalysisPrivateContent`

Minimum fields:

- `analysis_id` — primary key and FK to `analyses.id`;
- `source_ciphertext` — nullable binary;
- `result_ciphertext` — nullable binary;
- `source_format_version`;
- `result_format_version`;
- `source_delete_after` — timezone-aware and indexed;
- `source_deleted_at`;
- `result_deleted_at`;
- `created_at`;
- `updated_at`.

The encrypted source payload must contain private source/context fields required to run an analysis:

- normalized messages;
- participants;
- selected user participant;
- user goal;
- relationship stage.

The encrypted result payload contains the validated structured report result.

The `analyses` row may retain only non-content metadata required for product operations and reconciliation, such as:

- status;
- intake step;
- source type;
- message and character counts;
- model/prompt/token/latency metadata;
- report access;
- cost units;
- financial transaction reference;
- timestamps and safe failure codes.

After Milestone 6 application writes, the legacy plaintext content columns must remain `NULL`.

Do not store ciphertext in JSON or text columns when a binary column is appropriate.

---

# 3. Repository and service boundaries

Do not scatter encryption and decryption through Telegram handlers.

Create typed source/result value objects and a focused repository/service boundary responsible for:

- encrypting source content before persistence;
- decrypting source content only for an owned active analysis that needs processing;
- encrypting a validated result before persistence;
- decrypting a result only for an owned report request;
- clearing source content;
- clearing all private content;
- reporting content state without returning plaintext.

Update all application paths so they no longer read or write the legacy plaintext fields directly, except the explicit legacy backfill command.

At minimum update:

- conversation intake and reset;
- analysis processing load/complete/fail paths;
- report loading and rendering;
- report history reopening;
- preview/full-access flows;
- single-analysis deletion;
- all-data deletion;
- tests and fixtures.

Do not pass decrypted conversation or result data into analytics properties.

Keep provider network I/O outside database transactions.

---

# 4. Reversible migration and legacy backfill

Add a new reversible Alembic revision after `20260803_07`, preferably:

`20260804_08_privacy_deletion_retention.py`

The migration must:

- create private encrypted-content storage;
- add user tombstone/deletion fields;
- make Telegram identity/profile fields nullable where required for anonymization;
- add analysis deletion timestamp/state fields needed by the implementation;
- add retention indexes;
- update constraints safely;
- downgrade cleanly to `20260803_07`.

Do not edit deployed migrations `20260803_01` through `20260803_07`.

Because Alembic must not require a production secret, do not attempt to encrypt legacy rows inside schema migration code.

Add an idempotent one-shot command, for example:

```bash
python -m app.cli.backfill_private_content
```

Required backfill behavior:

- process rows in bounded batches;
- use `FOR UPDATE SKIP LOCKED` where useful;
- encrypt legacy source and result fields;
- clear plaintext columns in the same transaction as the encrypted insert/update;
- be safe to restart;
- skip already migrated rows;
- detect conflicting encrypted and plaintext values rather than silently overwriting either;
- support a dry-run/count mode;
- log only counts and safe internal identifiers;
- never log private field values;
- exit non-zero when unresolved conflicts remain.

Add PostgreSQL tests for backfill restartability and conflicting state.

---

# 5. Source-content retention

`RAW_CONTENT_RETENTION_DAYS` already exists and defaults to 30 days. Make it operational.

When a valid conversation source is persisted or replaced:

- set `source_delete_after = now + RAW_CONTENT_RETENTION_DAYS`;
- store the source only in encrypted form;
- never extend the deadline merely because a report is reopened;
- resetting and submitting genuinely new source content may set a new deadline for that new source.

Add an idempotent one-shot cleanup command/job, for example:

```bash
python -m app.cli.retention_cleanup
```

Required behavior:

- bounded batches;
- `FOR UPDATE SKIP LOCKED`;
- dry-run mode;
- safe concurrent execution by multiple workers;
- delete expired encrypted source and any legacy plaintext source;
- set `source_deleted_at`;
- preserve encrypted completed report results so history still works;
- preserve non-content metadata and financial references;
- completed analyses remain reopenable after source cleanup;
- an expired draft/queued/processing analysis that cannot safely continue without source must become deleted or another explicit terminal privacy-safe state;
- no content may be resurrected by a concurrent stale analysis worker;
- repeated cleanup runs are no-ops;
- emit privacy-safe counts only.

Document how to schedule this command with cron, a managed scheduled job or an equivalent production scheduler. Do not add a busy polling loop solely for retention.

---

# 6. Immediate deletion of one analysis

Expand single-analysis deletion to every relevant state:

- draft;
- queued;
- processing;
- completed;
- failed;
- already deleted.

Required behavior:

- verify ownership;
- lock rows in a consistent order;
- clear encrypted source and result;
- clear every legacy plaintext content field;
- clear feedback and report access;
- set `status='deleted'` and `deleted_at`;
- preserve only safe metadata and financial references required for ledger reconciliation;
- do not refund credits merely because the user requested deletion;
- release a free-preview reservation when it is still reserved by this analysis and has not been consumed;
- make repeated and concurrent delete requests idempotent;
- prevent a stale processing worker from restoring the result after deletion;
- prevent history, replies and follow-up handlers from reopening deleted content.

Do not require a completed report before deletion.

---

# 7. Delete all user data with a financial tombstone

Add a dedicated transactional `DataDeletionService` or equivalent.

A user must be able to delete all non-required personal data from Telegram.

## User tombstone

Preserve the internal user UUID only because immutable financial tables reference it.

On completed deletion, clear or null:

- Telegram user ID;
- Telegram username;
- first name;
- language;
- age confirmation fields;
- consent fields;
- onboarding state;
- preview analysis reference;
- any other profile/identity data.

Set explicit tombstone fields such as:

- `privacy_status='deleted'`;
- `deleted_at`.

Database constraints must guarantee that an active user has required Telegram identity, while a deleted tombstone has no Telegram/profile identity.

After deletion, a later `/start` from the same Telegram account must create a new independent active user rather than reconnecting the tombstone.

## Analyses

For every analysis belonging to the user:

- delete encrypted source and result;
- clear legacy plaintext fields;
- mark deleted;
- clear feedback and report access;
- preserve only safe non-content metadata and financial references.

## Billing and ledger

Never delete or rewrite immutable `CreditTransaction` history.

Retain only the minimum payment facts needed for internal reconciliation, including as applicable:

- provider;
- payment/order IDs;
- product code/version;
- amount and currency;
- timestamps;
- credit ledger relationships;
- safe status and error codes.

Immediately clear:

- encrypted receipt contacts;
- hosted checkout URLs;
- encrypted payment methods;
- any provider/customer metadata not required for reconciliation;
- any private content accidentally present in snapshots or payloads.

Cancel or terminate local active checkout state so no new credit can be granted to a deleted tombstone.

Payment completion and checkout creation must fail closed for deleted users.

A payment worker that was already in flight must re-check the locked User tombstone state before granting credits.

Keep existing provider transaction identifiers only when required for financial reconciliation. Document the exact retained-data matrix and rationale without making unsupported legal claims.

## Concurrency and idempotency

All-data deletion must be idempotent and safe when racing with:

- analysis completion;
- report access finalization;
- checkout creation;
- payment completion;
- webhook processing;
- reconciliation;
- retention cleanup;
- another all-data deletion request.

After deletion commits:

- no analysis source or result exists;
- no Telegram/profile identity remains on the tombstone;
- no pending checkout can grant new credits;
- no stale worker can restore content;
- ledger and payment records remain internally consistent.

Unused credits cannot be reassociated after identity deletion. The final confirmation UI must state this clearly before the user confirms deletion.

---

# 8. Telegram privacy and deletion UI

Replace the `menu:privacy` placeholder with a real Russian-language flow.

The privacy screen must explain, in plain language:

- conversation source is encrypted at rest;
- source retention duration from configuration;
- the user can delete an individual analysis immediately;
- the user can delete all account data;
- minimal payment/ledger records may remain in anonymous/tombstoned form for reconciliation;
- deleted content is not available for restoration;
- deleting the account disconnects unused credits from the Telegram identity.

Add buttons for:

- return to menu;
- delete all data;
- confirmation;
- cancellation.

Use a two-step destructive confirmation. Do not place Telegram identity, source text, report text or receipt contact in callback data or FSM data.

On successful all-data deletion:

- clear FSM state;
- send a final confirmation;
- do not render authenticated account data;
- instruct the user that `/start` creates a new independent account.

Keep all user-facing strings centralized.

Test actual aiogram handlers with `MemoryStorage` and a recording Telegram session.

---

# 9. Logging and analytics privacy

Add regression protection proving that sensitive content is absent from logs and analytics on both success and error paths.

Requirements:

- no raw message text;
- no normalized message text;
- no generated report text;
- no user goal;
- no participant names;
- no receipt contact;
- no encryption key;
- no ciphertext dumps.

Structured logs may contain:

- internal analysis/user/order UUIDs;
- safe status values;
- batch counts;
- safe failure codes;
- timing buckets.

Add or update privacy-safe analytics events:

- `analysis_deleted` once per transition;
- `all_data_deleted` once per completed tombstone transition;
- optional aggregate retention cleanup counters without user content.

Do not send Telegram user ID in deletion analytics.

---

# 10. Required PostgreSQL and concurrency tests

Use real PostgreSQL and separate sessions for concurrency. Reuse isolated-schema patterns.

At minimum add:

## Encryption and storage

- same plaintext produces different ciphertexts;
- ciphertext decrypts correctly;
- tampering fails;
- wrong purpose/key fails;
- no plaintext source/result columns are populated by new writes;
- source and report can be loaded through the encrypted repository;
- report history works after source retention cleanup.

## Backfill

- legacy source and result are encrypted and plaintext cleared atomically;
- repeated backfill is a no-op;
- interrupted batches resume safely;
- conflicting encrypted/plaintext state is detected without data loss.

## Single-analysis deletion

For each relevant status:

- content is removed;
- analysis is durably deleted;
- financial reference is preserved;
- repeated deletion is idempotent;
- ownership is enforced;
- reserved preview is safely released when appropriate.

## All-data deletion

- Telegram/profile fields are null on the tombstone;
- all analyses have no source/result content;
- legacy plaintext is null;
- receipt contact and checkout URL are cleared;
- active local checkout cannot later grant credits;
- CreditTransaction row count, amounts and immutable IDs are unchanged;
- payment reconciliation fields remain coherent;
- a new `/start` creates a new active user UUID;
- repeated deletion is idempotent.

## Races

Repeat important races at least 25 times:

- analysis completion versus analysis deletion;
- payment completion versus all-data deletion;
- all-data deletion versus retention cleanup.

After every iteration assert:

- zero resurrected private content;
- no post-deletion credit grant;
- no duplicate ledger entries;
- valid terminal states;
- no deadlock or uncaught integrity error.

## Retention workers

- multiple cleanup workers use `SKIP LOCKED`;
- one expired source is cleared once;
- completed result remains readable;
- expired incomplete analysis becomes privacy-safe terminal state;
- dry-run changes nothing;
- repeated cleanup is a no-op.

## Telegram handlers

Test actual handlers for:

- privacy screen;
- delete-all prompt;
- cancellation;
- successful confirmation;
- already-deleted/idempotent confirmation;
- FSM cleanup;
- no sensitive values in callback/FSM/logs.

## Logging

Use a unique sentinel in a conversation, goal, participant name, result and receipt contact. Assert it never appears in captured logs or analytics properties on success and failure paths.

---

# 11. Documentation

Update README and add focused privacy documentation covering:

- encrypted data model;
- source versus result retention;
- immediate deletion behavior;
- all-data tombstone behavior;
- exact retained financial fields/categories;
- backfill deployment order;
- retention cleanup command and scheduling;
- key management and rotation limitations;
- recovery limitations after deletion;
- operational verification queries that return counts only, never content.

Document a safe rollout order:

1. deploy schema migration;
2. deploy code that writes encrypted private content;
3. run and verify backfill;
4. verify no legacy plaintext remains;
5. schedule retention cleanup.

Do not put real secrets or example private conversation content in documentation.

---

# 12. Validation

Run from:

`ideas/ai-relationship-platform`

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

All commands must pass before publishing.

The full suite baseline before this milestone is 311 passing tests. The final total should be higher.

---

# 13. Delivery

Before publishing, delete this local task file:

`ideas/ai-relationship-platform/CODEX_TASK_M6.md`

It must not appear in the final pull request diff.

Commit and push all implementation changes to the existing branch:

`codex/milestone-6-privacy-deletion-retention`

Open one pull request targeting:

`main`

Title:

`Implement Milestone 6 privacy deletion and retention`

Do not create a duplicate PR.

Do not merge.

The PR description must contain:

- actual base SHA;
- final head SHA;
- migration revision;
- exact changed-file count;
- exact total and new-test counts;
- encryption format/library and purpose separation;
- legacy backfill results;
- plaintext verification result;
- single-analysis deletion results;
- all-data tombstone results;
- financial ledger preservation result;
- 25-iteration analysis/deletion race result;
- 25-iteration payment/deletion race result;
- retention concurrency result;
- actual Telegram privacy handler results;
- log/analytics sentinel result;
- Ruff, strict mypy, Alembic and Docker Compose results;
- final GitHub Actions run number and conclusion;
- confirmation that no real external credentials, provider calls or payments were used;
- confirmation that the PR was not merged.

Correct completion statement:

> Milestone 6 privacy, deletion and retention are implemented and verified. Sensitive analysis source and result content are encrypted at rest, user-requested deletion is durable, expired source content is removed automatically, and immutable financial records remain reconcilable through an identity-free tombstone.

Do not report completion until the final GitHub Actions run is fully green.