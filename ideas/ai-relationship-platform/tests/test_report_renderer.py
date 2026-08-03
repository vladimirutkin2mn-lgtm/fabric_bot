"""Provider-independent report rendering and chunking regression tests."""
# ruff: noqa: RUF001

from app.domain.analysis import AnalysisResult
from app.providers.llm.base import LLMRequest
from app.providers.llm.stub import StubLLMClient
from app.services.report_renderer import ReportRenderer, chunk_text, confidence_label


async def result() -> AnalysisResult:
    completion = await StubLLMClient().generate_analysis(
        LLMRequest("s", "u", AnalysisResult.model_json_schema(), ("m1",), ("A", "B"))
    )
    return AnalysisResult.model_validate_json(completion.payload)


async def test_complete_report_sections_labels_and_evidence() -> None:
    rendered = "".join(ReportRenderer().render(await result()).chunks)
    headings = [
        "Общий вывод",
        "Что видно в переписке",
        "Куда движется общение",
        "Наблюдаемая взаимность",
        "Возможные объяснения",
        "Чего нельзя понять по этой переписке",
        "Что делать дальше",
        "Варианты ответа",
    ]
    assert [rendered.index(value) for value in headings] == sorted(
        rendered.index(value) for value in headings
    )
    assert "сообщение №1" in rendered
    assert "mixed" not in rendered and "0.6" not in rendered and "{" not in rendered
    assert "Это гипотезы, а не установленные факты." in rendered
    assert "не измерение чувств" in rendered


async def test_safety_insufficient_and_unknown_codes_are_sanitized() -> None:
    import json

    payload = (await result()).model_dump(mode="json")
    payload["quality"] = {
        "sufficient": False,
        "issues": ["SECRET_INTERNAL_ISSUE"],
        "participants_detected": ["A", "B"],
    }
    payload["safety"] = {"high_risk_detected": True, "categories": ["SECRET_INTERNAL_CATEGORY"]}
    value = AnalysisResult.model_validate_json(json.dumps(payload))
    rendered = "".join(ReportRenderer().render(value).chunks)
    assert rendered.startswith("Важно о безопасности")
    assert "Данных недостаточно" in rendered
    assert "SECRET_INTERNAL" not in rendered


def test_confidence_thresholds() -> None:
    assert [confidence_label(value) for value in (0.0, 0.39, 0.4, 0.69, 0.7, 1.0)] == [
        "низкая",
        "низкая",
        "средняя",
        "средняя",
        "высокая",
        "высокая",
    ]


def test_chunking_preserves_unicode_and_long_unbroken_content() -> None:
    source = "Заголовок\n\n" + "🙂абв" * 3000 + "X" * 5000
    chunks = chunk_text(source)
    assert chunks and all(0 < len(chunk) <= 4096 for chunk in chunks)
    assert "".join(chunks) == source


def test_invalid_chunk_configuration_is_rejected() -> None:
    import pytest

    for target, hard_limit in ((0, 1), (1, 0), (-1, 4), (5, 4)):
        with pytest.raises(ValueError, match="invalid_chunk_limits"):
            chunk_text("text", target, hard_limit)


async def test_all_dynamic_direction_importance_and_reply_labels() -> None:
    import json

    directions = {
        "warming": "становится теплее",
        "stable_positive": "стабильно позитивное",
        "mixed": "смешанное или неясное",
        "cooling": "становится холоднее",
        "unstable": "нестабильное",
        "insufficient_data": "недостаточно данных",
    }
    base = (await result()).model_dump(mode="json")
    for code, label in directions.items():
        value = dict(base)
        value["dynamic"] = {"direction": code, "confidence": 0.5}
        text = "".join(
            ReportRenderer().render(AnalysisResult.model_validate_json(json.dumps(value))).chunks
        )
        assert label in text and code not in text
    for code, label in {"low": "низкая", "medium": "средняя", "high": "высокая"}.items():
        value = dict(base)
        value["observations"] = [{"claim": "Сигнал", "evidence_refs": ["m1"], "importance": code}]
        text = "".join(
            ReportRenderer().render(AnalysisResult.model_validate_json(json.dumps(value))).chunks
        )
        assert f"Важность: {label}" in text
    styles = {
        "warm_direct": "тепло и прямо",
        "light_low_pressure": "легко и без давления",
        "boundary_setting": "с обозначением границ",
    }
    for code, label in styles.items():
        value = dict(base)
        value["reply_suggestions"] = [{"style": code, "text": "Ответ", "why_it_fits": "Причина"}]
        text = "".join(
            ReportRenderer().render(AnalysisResult.model_validate_json(json.dumps(value))).chunks
        )
        assert label in text and code not in text


async def test_empty_sections_multiple_evidence_cyrillic_emoji_and_determinism() -> None:
    import json

    base = (await result()).model_dump(mode="json")
    base.update(
        {
            "summary": "Кириллица 🙂",
            "observations": [],
            "hypotheses": [],
            "unknowns": [],
            "next_actions": [],
            "reply_suggestions": [],
        }
    )
    value = AnalysisResult.model_validate_json(json.dumps(base, ensure_ascii=False))
    renderer = ReportRenderer()
    assert renderer.render(value) == renderer.render(value)
    text = "".join(renderer.render(value).chunks)
    assert "Кириллица 🙂" in text and "пока нет" in text
