"""Pure, provider-independent Russian report rendering and safe text chunking."""
# ruff: noqa: RUF001, RUF002, E501

from dataclasses import dataclass

from app.domain.analysis import AnalysisResult, Confidence, DynamicDirection, Importance, ReplyStyle

CHUNK_TARGET = 3700
TELEGRAM_LIMIT = 4096

DIRECTION_LABELS = {
    DynamicDirection.WARMING: "становится теплее",
    DynamicDirection.STABLE_POSITIVE: "стабильно позитивное",
    DynamicDirection.MIXED: "смешанное или неясное",
    DynamicDirection.COOLING: "становится холоднее",
    DynamicDirection.UNSTABLE: "нестабильное",
    DynamicDirection.INSUFFICIENT_DATA: "недостаточно данных",
}
CONFIDENCE_LABELS = {
    Confidence.LOW: "низкая",
    Confidence.MEDIUM: "средняя",
    Confidence.HIGH: "высокая",
}
IMPORTANCE_LABELS = {
    Importance.LOW: "низкая",
    Importance.MEDIUM: "средняя",
    Importance.HIGH: "высокая",
}
REPLY_STYLE_LABELS = {
    ReplyStyle.WARM_DIRECT: "тепло и прямо",
    ReplyStyle.LIGHT_LOW_PRESSURE: "легко и без давления",
    ReplyStyle.BOUNDARY_SETTING: "с обозначением границ",
}
RELATIONSHIP_STAGE_LABELS = {
    "new_connection": "Только познакомились",
    "dating": "Ходим на свидания",
    "relationship": "В отношениях",
    "post_breakup": "После расставания",
    "unclear": "Сложно определить",
    "not_provided": "Стадия не указана",
}
QUALITY_ISSUE_LABELS = {
    "too_short": "слишком мало сообщений",
    "one_sided": "переписка односторонняя",
    "missing_context": "не хватает контекста",
    "insufficient_context": "не хватает контекста",
}
SAFETY_CATEGORY_LABELS = {
    "threats": "угрозы",
    "coercion": "давление или принуждение",
    "stalking": "преследование",
    "blackmail": "шантаж",
    "violence": "риск насилия",
}


@dataclass(frozen=True)
class RenderedReport:
    chunks: tuple[str, ...]


def confidence_label(value: float) -> str:
    """Map 0–.39/.40–.69/.70–1 to low/medium/high without fake precision."""
    return "низкая" if value < 0.4 else "средняя" if value < 0.7 else "высокая"


def _refs(values: list[str]) -> str:
    numbers = [value[1:] for value in values]
    if not numbers:
        return "нет ссылок на отдельные сообщения"
    joined = f"{', '.join(numbers[:-1])} и {numbers[-1]}" if len(numbers) > 1 else numbers[0]
    noun = "сообщения" if len(numbers) > 1 else "сообщение"
    return f"{noun} №{joined.replace(', ', ', №').replace(' и ', ' и №')}"


def chunk_text(
    text: str, target: int = CHUNK_TARGET, hard_limit: int = TELEGRAM_LIMIT
) -> tuple[str, ...]:
    """Split deterministically at section/paragraph/line/space boundaries without loss."""
    if target <= 0 or hard_limit <= 0 or target > hard_limit:
        raise ValueError("invalid_chunk_limits")
    if not text:
        return ()
    chunks: list[str] = []
    remaining = text
    while len(remaining) > hard_limit:
        window = remaining[:target]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "), window.rfind(" "))
        if cut < target // 2:
            cut = target
        elif window[cut : cut + 2] == ". ":
            cut += 1
        piece = remaining[:cut]
        if piece:
            chunks.append(piece)
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def _bullets(values: list[str], fallback: str) -> str:
    return "\n".join(f"• {value}" for value in values) if values else fallback


class ReportRenderer:
    """Render only a validated AnalysisResult; no transport or persistence dependencies."""

    def render(self, result: AnalysisResult) -> RenderedReport:
        sections: list[str] = []
        if result.safety.high_risk_detected:
            categories = [
                SAFETY_CATEGORY_LABELS.get(value, "другие потенциально опасные сигналы")
                for value in result.safety.categories
            ]
            notice = (
                "Важно о безопасности\n\nВ переписке могут быть признаки потенциально небезопасного поведения. "
                "Если есть угрозы, давление, преследование, шантаж или риск насилия, "
                "приоритет — ваша безопасность и помощь людей или служб, которым вы доверяете."
            )
            if categories:
                notice += "\nОбратите внимание: " + ", ".join(dict.fromkeys(categories)) + "."
            sections.append(notice)
        if not result.quality.sufficient:
            issues = [
                QUALITY_ISSUE_LABELS.get(value, "качество данных ограничено")
                for value in result.quality.issues
            ]
            notice = "Данных недостаточно для уверенного вывода."
            if issues:
                notice += "\nПричины: " + "; ".join(dict.fromkeys(issues)) + "."
            sections.append(notice)
        sections.append(
            "Общий вывод\n\n"
            + result.summary
            + "\nУверенность: "
            + confidence_label(result.dynamic.confidence)
            + ".\nОтчёт описывает наблюдаемое общение, а не скрытые чувства человека."
        )
        observations = []
        for index, observation in enumerate(result.observations, 1):
            observations.append(
                f"{index}. {observation.claim}\nВажность: {IMPORTANCE_LABELS[observation.importance]}. Основание: {_refs(observation.evidence_refs)}."
            )
        sections.append(
            "Что видно в переписке\n\n"
            + ("\n\n".join(observations) or "Надёжных отдельных наблюдений пока нет.")
        )
        sections.append(
            f"Куда движется общение\n\n{DIRECTION_LABELS[result.dynamic.direction]}.\nУверенность: {confidence_label(result.dynamic.confidence)}."
        )
        score = result.reciprocity_score
        sections.append(
            "Наблюдаемая взаимность\n\n"
            + f"{score.value} из 100\nЭто оценка видимых сигналов общения, а не измерение чувств человека.\n\nПозитивные сигналы:\n{_bullets(score.positive_signals, 'Не выделены.')}\n\nНегативные сигналы:\n{_bullets(score.negative_signals, 'Не выделены.')}\n\nОграничения:\n{_bullets(score.limitations, 'Оценка ограничена доступным фрагментом переписки.')}"
        )
        hypotheses = []
        for index, hypothesis in enumerate(result.hypotheses, 1):
            hypotheses.append(
                f"{index}. Гипотеза: {hypothesis.label}\n{hypothesis.explanation}\nУверенность: {CONFIDENCE_LABELS[hypothesis.confidence]}.\nПоддерживают: {_refs(hypothesis.supporting_evidence_refs)}.\nПротиворечат: {_refs(hypothesis.contradicting_evidence_refs)}."
            )
        sections.append(
            "Возможные объяснения\n\nЭто гипотезы, а не установленные факты.\n\n"
            + ("\n\n".join(hypotheses) or "Обоснованных гипотез недостаточно.")
        )
        sections.append(
            "Чего нельзя понять по этой переписке\n\n"
            + _bullets(
                result.unknowns,
                "Дополнительные существенные неизвестные не выделены; вывод всё равно ограничен перепиской.",
            )
        )
        actions = [
            f"{i}. {item.action}\nПочему может помочь: {item.why}\nРиск или осторожность: {item.risk}"
            for i, item in enumerate(result.next_actions[:3], 1)
        ]
        sections.append(
            "Что делать дальше\n\n"
            + ("\n\n".join(actions) or "Безопасный следующий шаг пока нельзя предложить.")
        )
        replies = [
            f"{i}. Стиль: {REPLY_STYLE_LABELS[item.style]}\n«{item.text}»\nПочему подходит: {item.why_it_fits}"
            for i, item in enumerate(result.reply_suggestions[:3], 1)
        ]
        sections.append(
            "Варианты ответа\n\n"
            + ("\n\n".join(replies) or "Подходящих вариантов ответа в сохранённом отчёте нет.")
        )
        return RenderedReport(chunk_text("\n\n".join(sections)))

    def render_replies(self, result: AnalysisResult) -> RenderedReport:
        """Render only already-persisted suggestions without invoking a provider."""
        replies = [
            f"{i}. Стиль: {REPLY_STYLE_LABELS[item.style]}\n«{item.text}»\n"
            f"Почему подходит: {item.why_it_fits}"
            for i, item in enumerate(result.reply_suggestions[:3], 1)
        ]
        text = "\n\n".join(replies) or "В сохранённом отчёте нет вариантов ответа."
        return RenderedReport(chunk_text(text))
