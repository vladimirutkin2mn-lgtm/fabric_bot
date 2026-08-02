"""Durable conversation intake business rules."""

from uuid import UUID

from app.db.models import Analysis, User
from app.providers.analytics import AnalyticsClient
from app.repositories.analyses import AnalysisRepository
from app.services.conversation_parser import ConversationParser, ParsedConversation


class InvalidTransition(ValueError):
    pass


class ConversationIntakeService:
    def __init__(
        self,
        analyses: AnalysisRepository,
        parser: ConversationParser,
        analytics: AnalyticsClient,
        goal_limit: int = 500,
    ) -> None:
        self._analyses, self._parser, self._analytics, self._goal_limit = (
            analyses,
            parser,
            analytics,
            goal_limit,
        )

    async def start(self, user: User) -> Analysis:
        analysis, created = await self._analyses.create_or_resume(user.id)
        if created:
            await self._analytics.track(str(user.id), "analysis_started", {"source_type": "text"})
        return analysis

    async def active(self, user_id: UUID) -> Analysis | None:
        return await self._analyses.get_active(user_id)

    async def submit(self, analysis: Analysis, content: str) -> ParsedConversation:
        if analysis.intake_step != "waiting_for_conversation":
            raise InvalidTransition("Conversation is not expected")
        parsed = self._parser.parse(content)
        analysis.normalized_conversation_json = parsed.message_dicts()
        analysis.participants_json = parsed.participants
        analysis.message_count, analysis.character_count = (
            parsed.message_count,
            parsed.character_count,
        )
        analysis.intake_step = "waiting_for_participant"
        await self._analyses.save(analysis)
        properties = {
            "source_type": "text",
            "source_format": parsed.source_format,
            "message_count_bucket": str(parsed.message_count),
            "character_count_bucket": str(parsed.character_count // 1000),
        }
        await self._analytics.track(str(analysis.user_id), "conversation_submitted", properties)
        await self._analytics.track(str(analysis.user_id), "conversation_parsed", properties)
        return parsed

    async def participant(self, analysis: Analysis, label: str) -> None:
        if analysis.intake_step == "waiting_for_goal" and analysis.user_participant_label == label:
            return
        if (
            analysis.intake_step != "waiting_for_participant"
            or not analysis.participants_json
            or label not in analysis.participants_json
        ):
            raise InvalidTransition("Invalid participant selection")
        analysis.user_participant_label, analysis.intake_step = label, "waiting_for_goal"
        await self._analyses.save(analysis)

    async def goal(self, analysis: Analysis, goal: str) -> None:
        clean = goal.strip()
        if analysis.intake_step != "waiting_for_goal" or not clean or len(clean) > self._goal_limit:
            raise InvalidTransition("Invalid goal")
        analysis.user_goal, analysis.intake_step = clean, "waiting_for_relationship_stage"
        await self._analyses.save(analysis)

    async def relationship_stage(self, analysis: Analysis, code: str) -> None:
        allowed = {
            "new_connection",
            "dating",
            "relationship",
            "post_breakup",
            "unclear",
            "not_provided",
        }
        if analysis.intake_step == "complete" and analysis.relationship_stage == code:
            return
        if analysis.intake_step != "waiting_for_relationship_stage" or code not in allowed:
            raise InvalidTransition("Invalid relationship stage")
        analysis.relationship_stage, analysis.intake_step = code, "complete"
        await self._analyses.save(analysis)
        await self._analytics.track(
            str(analysis.user_id), "analysis_context_completed", {"relationship_stage_code": code}
        )

    async def cancel(self, analysis: Analysis) -> None:
        if analysis.status != "deleted":
            await self._analyses.cancel(analysis)
            await self._analytics.track(str(analysis.user_id), "analysis_cancelled")
