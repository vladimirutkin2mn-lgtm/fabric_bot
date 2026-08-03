"""Provider-independent report rendering and chunking regression tests."""
# ruff: noqa: RUF001

import json

from app.domain.analysis import AnalysisResult
from app.providers.llm.base import LLMRequest
from app.providers.llm.stub import StubLLMClient
from app.services.report_renderer import ReportRenderer, chunk_text, confidence_label


async def result() -> AnalysisResult:
    completion = await StubLLMClient().generate_analysis(
        LLMRequest("s", "u", AnalysisResult.model_json_schema(), ("m1",), ("A", "B"))
    )
    return AnalysisResult.model_validate(json.loads(completion.payload))


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
    value = await result()
    value.quality.sufficient = False
    value.quality.issues = ["SECRET_INTERNAL_ISSUE"]
    value.safety.high_risk_detected = True
    value.safety.categories = ["SECRET_INTERNAL_CATEGORY"]
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
