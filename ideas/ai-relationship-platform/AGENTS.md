# AGENTS.md — HeartSignal

## Mission

Build an MVP Telegram-first product that analyzes relationship conversations and returns a structured, probabilistic interpretation of communication dynamics plus practical next steps.

Core promise:

> Forward or paste a conversation. The product explains the observable signals, how interest appears to be changing, what remains uncertain, and what the user can do next.

This is not a mind-reading or diagnosis product. Never present inferred feelings, motives, personality traits, or future outcomes as facts.

## Source of truth

Before changing code, read in this order:

1. `PRODUCT_SPEC.md`
2. `TASKS.md`
3. Existing code and tests

When the documents conflict, follow `PRODUCT_SPEC.md`. When implementation details are missing, choose the simplest production-sensible option and document the decision.

## Working rules

- Implement one milestone from `TASKS.md` at a time.
- Do not silently expand scope.
- Prefer a working vertical slice over broad scaffolding.
- Keep external services behind interfaces: LLM, payments, analytics, storage.
- Never invent SDK methods or API fields. Check the currently installed package version and official documentation.
- Add or update tests for every behavior change.
- Run formatting, linting, type checks, and tests before marking a task complete.
- Keep secrets out of the repository. Maintain `.env.example`.
- Use migrations for database changes.
- Log structured events without storing raw private conversations in application logs.
- Make deletion possible: a user must be able to delete an analysis and all raw source content associated with it.

## Default technical direction

Unless the repository already contains a coherent alternative stack, use:

- Python 3.12
- FastAPI for webhook and health endpoints
- aiogram 3 for Telegram
- PostgreSQL
- SQLAlchemy 2 async + Alembic
- Pydantic 2
- pytest
- Ruff
- mypy or pyright
- Docker Compose for local development

Use a provider-neutral `LLMClient` interface. The first implementation may use the OpenAI SDK, but domain logic must not depend directly on it.

## Product language

The first interface language is Russian. Keep all user-facing strings centralized so localization is easy.

## Analysis quality rules

Every generated analysis must:

- separate observations from interpretations;
- cite concrete message patterns from the supplied conversation using short paraphrases or message references;
- report uncertainty;
- avoid deterministic claims such as “he loves you,” “she is cheating,” or “they will return”;
- avoid medical or psychological diagnoses;
- avoid manipulative, coercive, humiliating, threatening, or deceptive recommendations;
- provide at most three next actions;
- provide at most three suggested replies;
- clearly state when the available conversation is too short or one-sided.

## Privacy defaults

- Store raw conversation content only when required for the analysis flow.
- Add a configurable retention period; default to 30 days.
- Allow immediate deletion.
- Never use user content for training by default.
- Do not expose one user’s content to another user.

## Definition of done for each milestone

A milestone is complete only when:

1. Acceptance criteria in `TASKS.md` pass.
2. Automated tests pass.
3. The happy path works locally using documented commands.
4. Errors are handled with a useful user-facing message.
5. README or setup documentation reflects the change.
