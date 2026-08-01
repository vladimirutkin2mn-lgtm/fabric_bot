# HeartSignal — Product Specification

## 1. Product concept

HeartSignal is a Telegram-first AI product for people experiencing uncertainty in romantic communication.

The product accepts a pasted or forwarded conversation and returns a structured report:

- observable communication signals;
- estimated direction of interest over time;
- balance of initiative and emotional investment;
- possible explanations, explicitly marked as hypotheses;
- uncertainty and missing context;
- recommended next action;
- suggested message variants.

Working positioning:

> Send the conversation. HeartSignal will show what is actually visible in the messages, what cannot be known, and what to do next.

The emotional entry point is curiosity and uncertainty. The retained value is an ongoing decision assistant for new relationship events.

## 2. Target user

Primary user:

- 18+;
- currently dating, in a relationship, or processing a breakup;
- repeatedly rereads messages and asks friends what another person meant;
- wants a quick, private, concrete interpretation;
- discovers the product through short-form content and enters through Telegram.

Initial use cases:

1. “Does this person appear interested?”
2. “Has the communication become colder?”
3. “Should I write now or wait?”
4. “What should I reply?”
5. “What changed after the date?”
6. “What does this message from an ex likely mean?”

## 3. Product principles

1. **Evidence before interpretation.** Start with observable patterns.
2. **Probabilities, not certainty.** The model cannot know another person’s mind.
3. **Actionable output.** The user should leave with a clear next step.
4. **Fast time to value.** First useful result within a few minutes.
5. **Private by default.** Raw conversations are sensitive.
6. **No manipulative coaching.** Do not optimize for control, pressure, jealousy, deception, or emotional dependence.
7. **Telegram-first, platform-independent core.** Domain logic must later support web and mobile clients.

## 4. MVP scope

### Included

- Telegram onboarding.
- Consent and privacy notice.
- Paste or forward text messages.
- Optional context questions:
  - relationship stage;
  - user’s goal;
  - who is “me” in the transcript;
  - approximate date range.
- Conversation normalization.
- LLM-based structured analysis.
- Human-readable report in Telegram.
- Up to three suggested replies.
- Free preview and credit/paywall boundary.
- User account, credit balance, analyses history.
- Delete one analysis or all user data.
- Basic product analytics.
- Admin health and usage metrics.

### Explicitly excluded from first MVP

- Full Telegram account authorization and automatic reading of private chats.
- Continuous monitoring of a conversation.
- Voice message transcription.
- Screenshot OCR or image analysis.
- Dating app integrations.
- Native mobile apps.
- Public social features.
- “Compatibility scores” based on astrology or personality diagnosis.
- Guaranteed predictions about cheating, love, return of an ex, or future relationship outcomes.

These may be considered after text-based conversion and retention are validated.

## 5. Primary user flow

### 5.1 Entry

1. User opens Telegram bot from an ad or referral link.
2. Bot explains the value in one screen.
3. User confirms they are 18+ and agrees not to upload content they are not permitted to share.
4. Bot offers:
   - Analyze a conversation
   - View previous analyses
   - Buy credits
   - Privacy and deletion

### 5.2 New analysis

1. Bot asks the user to paste or forward the conversation.
2. Bot shows formatting guidance and a short example.
3. System parses participants and messages.
4. If participant identity is ambiguous, ask: “Which participant are you?”
5. Ask one high-value context question: “What do you want to understand?”
6. Optional: relationship stage.
7. Show estimated analysis cost in credits.
8. If user has insufficient credits, show free preview or paywall.
9. Run analysis.
10. Deliver the report in several Telegram messages with clear sections.
11. Offer:
    - Generate reply options
    - Ask a follow-up question
    - Analyze a newer fragment
    - Delete this analysis

### 5.3 Free preview

The free preview should deliver genuine value but preserve a reason to pay.

Free preview:

- conversation quality check;
- two strongest observable signals;
- one uncertainty;
- a blurred or summarized indication of the overall dynamic.

Paid report:

- full structured report;
- trend and initiative analysis;
- hypotheses;
- next actions;
- reply suggestions;
- one follow-up question.

## 6. Report structure

Every paid report must contain these sections.

### 6.1 Summary

A concise paragraph with the overall pattern and confidence level.

### 6.2 What is directly observable

3–7 evidence-based observations such as:

- who initiates more often;
- response delay pattern when timestamps exist;
- message length balance;
- question and follow-up frequency;
- warmth, humor, affection, and reciprocity;
- topic avoidance;
- abrupt changes over time;
- planning and follow-through;
- repair attempts after tension.

### 6.3 Direction of the dynamic

One of:

- warming;
- stable-positive;
- mixed/unclear;
- cooling;
- unstable;
- insufficient data.

Include a confidence value from 0 to 1, but do not show fake precision to the user. Map it to low, medium, or high confidence.

### 6.4 Interest indicators

Provide a bounded score for UX, but explain it as a communication-signal score, not a measurement of feelings.

Recommended label:

> Observable reciprocity score

Range: 0–100.

The score must be accompanied by:

- strongest positive signals;
- strongest negative signals;
- reasons the score may be misleading.

### 6.5 Plausible interpretations

At most three hypotheses. Each must include:

- explanation;
- supporting evidence;
- contradicting evidence;
- confidence: low / medium / high.

### 6.6 What cannot be concluded

Explicitly list important unknowns.

### 6.7 Recommended next step

At most three actions, ordered from safest and most informative to more assertive.

### 6.8 Suggested replies

Up to three variants:

- warm and direct;
- light and low-pressure;
- boundary-setting.

Only show variants that fit the user’s stated goal.

## 7. Structured LLM output

The LLM must return validated JSON matching a schema similar to:

```json
{
  "quality": {
    "sufficient": true,
    "issues": [],
    "participants_detected": ["A", "B"]
  },
  "summary": "string",
  "dynamic": {
    "direction": "warming|stable_positive|mixed|cooling|unstable|insufficient_data",
    "confidence": 0.0
  },
  "reciprocity_score": {
    "value": 0,
    "positive_signals": ["string"],
    "negative_signals": ["string"],
    "limitations": ["string"]
  },
  "observations": [
    {
      "claim": "string",
      "evidence_refs": ["m12", "m18"],
      "importance": "low|medium|high"
    }
  ],
  "hypotheses": [
    {
      "label": "string",
      "explanation": "string",
      "supporting_evidence_refs": ["m12"],
      "contradicting_evidence_refs": ["m30"],
      "confidence": "low|medium|high"
    }
  ],
  "unknowns": ["string"],
  "next_actions": [
    {
      "action": "string",
      "why": "string",
      "risk": "string"
    }
  ],
  "reply_suggestions": [
    {
      "style": "warm_direct|light_low_pressure|boundary_setting",
      "text": "string",
      "why_it_fits": "string"
    }
  ],
  "safety": {
    "high_risk_detected": false,
    "categories": []
  }
}
```

Validate model output. On validation failure, retry once with a repair prompt. If the second attempt fails, return a graceful error and preserve the user’s credit.

## 8. Conversation normalization

Represent each message as:

```json
{
  "id": "m1",
  "speaker": "A",
  "timestamp": "optional ISO-8601",
  "text": "message text",
  "source_order": 1
}
```

Parsing requirements:

- support common pasted formats;
- preserve order;
- strip obvious Telegram metadata noise;
- detect when all text belongs to one participant;
- reject empty content;
- cap the first MVP at a configurable number of characters or tokens;
- tell the user how to split oversized conversations;
- never mutate the meaning of the original messages during normalization.

## 9. Domain model

Minimum entities:

### User

- id
- telegram_user_id
- locale
- age_confirmed_at
- consent_version
- credits_balance
- created_at
- updated_at

### Analysis

- id
- user_id
- status: draft / queued / processing / completed / failed / deleted
- user_goal
- relationship_stage
- user_participant_label
- source_type
- raw_content_encrypted or raw_content_reference
- normalized_conversation_json
- result_json
- model_name
- prompt_version
- cost_units
- created_at
- completed_at
- delete_after

### CreditTransaction

- id
- user_id
- type: grant / purchase / spend / refund / adjustment
- amount
- analysis_id optional
- external_payment_id optional
- created_at

### Event

Analytics event storage may be external. If stored internally, do not include raw conversation content.

## 10. Service boundaries

Recommended modules:

- `bot/` — Telegram handlers, keyboards, state machine.
- `api/` — FastAPI webhook, health, admin endpoints.
- `domain/` — entities and business rules.
- `services/conversation_parser.py`
- `services/analysis_service.py`
- `services/report_renderer.py`
- `services/credits_service.py`
- `providers/llm/`
- `providers/payments/`
- `providers/analytics/`
- `repositories/`
- `db/`

Interfaces:

```python
class LLMClient(Protocol):
    async def analyze_conversation(self, request: AnalysisRequest) -> AnalysisResult: ...

class PaymentProvider(Protocol):
    async def create_checkout(self, user_id: UUID, product_code: str) -> Checkout: ...
    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> PaymentEvent: ...

class AnalyticsClient(Protocol):
    async def track(self, user_id: str | None, event: str, properties: dict) -> None: ...
```

## 11. Prompt design

Use versioned prompts stored as files, not giant string literals hidden in handlers.

System prompt requirements:

- analyze communication behavior, not hidden thoughts;
- distinguish evidence, interpretation, and unknowns;
- never diagnose mental illness or personality disorders;
- never claim certainty about love, cheating, lying, or future behavior;
- avoid gender stereotypes;
- do not reward manipulative behavior;
- flag threats, stalking, coercion, blackmail, or violence and recommend prioritizing safety;
- output only schema-valid JSON.

Use message IDs as evidence references. Do not reproduce large portions of private conversations in the report.

## 12. Monetization model for MVP

Use credits so pricing can be changed without redesigning the flow.

Suggested initial product codes:

- `analysis_single`
- `analysis_pack_5`
- `subscription_monthly`

Suggested credit logic:

- onboarding grant: 1 preview credit;
- full analysis: configurable credit amount;
- one follow-up question included in a paid analysis;
- failed technical analysis automatically refunds credits;
- repeat analysis of a new fragment costs credits.

The actual payment provider must be implemented behind `PaymentProvider`. A mock provider is required for local development.

## 13. Analytics events

Track at least:

- `bot_started`
- `onboarding_completed`
- `analysis_started`
- `conversation_submitted`
- `conversation_rejected`
- `preview_viewed`
- `paywall_viewed`
- `checkout_started`
- `purchase_completed`
- `analysis_processing_started`
- `analysis_completed`
- `analysis_failed`
- `reply_suggestions_requested`
- `followup_requested`
- `analysis_deleted`
- `all_data_deleted`

Key funnel:

`bot_started → conversation_submitted → preview_viewed → paywall_viewed → purchase_completed → analysis_completed`

Do not send raw messages or report text to analytics.

## 14. Error handling

User-facing cases:

- conversation too short;
- only one participant detected;
- unsupported format;
- conversation too large;
- insufficient credits;
- LLM timeout;
- invalid model response;
- payment verification failure;
- deletion failure.

Every error message should tell the user what to do next.

## 15. Security and privacy requirements

- Encrypt sensitive content at rest or store it in an encrypted managed service.
- Use HTTPS webhooks.
- Verify Telegram webhook secret where supported.
- Verify payment webhooks.
- Do not include private content in exception traces.
- Redact message text from logs.
- Add rate limits per Telegram user.
- Add idempotency for payment callbacks and analysis jobs.
- Add a retention cleanup job.

## 16. MVP success metrics

Primary validation metrics:

- start-to-submission conversion;
- submission-to-preview completion;
- preview-to-paid conversion;
- paid analysis completion rate;
- cost per completed analysis;
- percentage requesting a reply suggestion;
- 7-day repeat analysis rate;
- refund/error rate;
- user-reported usefulness after analysis.

A simple feedback question after each report:

> Насколько этот разбор помог понять ситуацию?

Scale 1–5 plus optional text.

## 17. Acceptance criteria for first usable release

The first usable release is complete when:

1. A new Telegram user can finish onboarding.
2. The user can paste a two-person text conversation.
3. The system normalizes and validates the content.
4. A mock or real LLM returns schema-valid analysis.
5. The bot renders a readable Russian report.
6. Credits are spent exactly once and refunded after technical failure.
7. A mock payment flow can add credits locally.
8. The user can list and delete analyses.
9. Raw content does not appear in logs or analytics.
10. The app starts through Docker Compose with documented commands.
11. Automated tests cover parser, credit idempotency, result validation, and the main Telegram flow.
