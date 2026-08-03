"""Deterministic, fictional local provider."""
# ruff: noqa: RUF001

import json
from typing import Literal

from app.providers.llm.base import (
    LLMAuthenticationError,
    LLMCompletion,
    LLMRateLimitError,
    LLMRequest,
    LLMTimeoutError,
    LLMTransientError,
)

StubBehavior = Literal[
    "success",
    "invalid_json",
    "invalid_schema",
    "invalid_evidence_ref",
    "timeout",
    "rate_limit",
    "authentication_error",
    "transport_error",
    "repair_success",
    "repair_failure",
]


class StubLLMClient:
    def __init__(self, model: str = "stub", behavior: StubBehavior = "success") -> None:
        self.model, self.behavior, self.calls = model, behavior, 0

    async def generate_analysis(self, request: LLMRequest) -> LLMCompletion:
        self.calls += 1
        if self.behavior == "timeout":
            raise LLMTimeoutError
        if self.behavior == "rate_limit":
            raise LLMRateLimitError
        if self.behavior == "authentication_error":
            raise LLMAuthenticationError
        if self.behavior == "transport_error":
            raise LLMTransientError
        invalid = self.behavior in {"invalid_json", "invalid_schema", "invalid_evidence_ref"}
        if self.behavior == "repair_success" and not request.repair:
            invalid = True
        if self.behavior == "repair_failure":
            invalid = True
        if invalid and self.behavior == "invalid_json":
            payload = "{not-json"
        elif invalid and self.behavior in {"invalid_schema", "repair_failure", "repair_success"}:
            payload = json.dumps({"summary": "неполный ответ"}, ensure_ascii=False)
        else:
            payload = json.dumps(self._result(request, invalid), ensure_ascii=False)
        return LLMCompletion(payload, "stub", self.model, "stub-request", 120, 240, 1)

    def _result(self, request: LLMRequest, invalid_ref: bool) -> dict[str, object]:
        ref = "m999" if invalid_ref else request.message_ids[0]
        return {
            "quality": {
                "sufficient": True,
                "issues": [],
                "participants_detected": list(request.participant_labels),
            },
            "summary": (
                "В диалоге заметен взаимный обмен репликами, "
                "но контекста для уверенных выводов мало."
            ),
            "dynamic": {"direction": "mixed", "confidence": 0.6},
            "reciprocity_score": {
                "value": 60,
                "positive_signals": ["Оба участника отвечают"],
                "negative_signals": [],
                "limitations": ["Короткий фрагмент"],
            },
            "observations": [
                {
                    "claim": "Участники поддерживают диалог.",
                    "evidence_refs": [ref],
                    "importance": "high",
                }
            ],
            "hypotheses": [
                {
                    "label": "Открытость к общению",
                    "explanation": "Ответы могут указывать на готовность продолжать разговор.",
                    "supporting_evidence_refs": [ref],
                    "contradicting_evidence_refs": [],
                    "confidence": "medium",
                }
            ],
            "unknowns": ["Неизвестен более широкий контекст общения."],
            "next_actions": [
                {
                    "action": "Задать спокойный открытый вопрос.",
                    "why": "Это даст больше наблюдаемых данных.",
                    "risk": "Ответ может быть кратким.",
                }
            ],
            "reply_suggestions": [
                {
                    "style": "light_low_pressure",
                    "text": "Как прошёл твой день?",
                    "why_it_fits": "Сообщение оставляет пространство для ответа.",
                }
            ],
            "safety": {"high_risk_detected": False, "categories": []},
        }
