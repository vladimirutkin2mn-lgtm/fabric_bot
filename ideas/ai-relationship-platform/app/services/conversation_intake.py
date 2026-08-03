"""Durable conversation intake business rules."""

from uuid import UUID

from app.db.models import Analysis, User
from app.providers.analytics import AnalyticsClient
from app.repositories.analyses import AnalysisRepository
from app.services.conversation_parser import (
    ConversationParser,
    ConversationRejected,
    ParsedConversation,
)


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

    async def pending_billing(self, user_id: UUID) -> Analysis | None:
        return await self._analyses.get_latest_pending_billing(user_id)

    async def owned(self, analysis_id: UUID, user_id: UUID) -> Analysis | None:
        """Return a draft owned by the user, including complete/deleted drafts."""
        return await self._analyses.get_owned(analysis_id, user_id)

    async def submit(self, analysis: Analysis, content: str) -> ParsedConversation:
        if analysis.intake_step != "waiting_for_conversation":
            raise InvalidTransition("Conversation is not expected")
        try:
            parsed = self._parser.parse(content)
        except ConversationRejected as error:
            await self._analytics.track(
                str(analysis.user_id),
                "conversation_rejected",
                {"rejection_reason": error.reason.value},
            )
            raise
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
        if analysis.intake_step == "waiting_for_relationship_stage" and analysis.user_goal == clean:
            return
        if analysis.intake_step != "waiting_for_goal" or not clean or len(clean) > self._goal_limit:
            raise InvalidTransition("Invalid goal")
        analysis.user_goal, analysis.intake_step = clean, "waiting_for_relationship_stage"
        await self._analyses.save(analysis)

    async def relationship_stage(self, analysis: Analysis, code: str) -> Analysis:
        allowed = {
            "new_connection",
            "dating",
            "relationship",
            "post_breakup",
            "unclear",
            "not_provided",
        }
        if analysis.intake_step == "complete" and analysis.relationship_stage == code:
            return analysis
        if analysis.intake_step != "waiting_for_relationship_stage" or code not in allowed:
            raise InvalidTransition("Invalid relationship stage")
        analysis.relationship_stage, analysis.intake_step = code, "complete"
        await self._analyses.save(analysis)
        await self._analytics.track(
            str(analysis.user_id), "analysis_context_completed", {"relationship_stage_code": code}
        )
        return analysis

    async def cancel(self, analysis: Analysis) -> None:
        if analysis.status == "deleted" and analysis.intake_step != "complete":
            return
        if analysis.status != "draft" or analysis.intake_step == "complete":
            raise InvalidTransition("Only an unfinished active draft can be cancelled")
        await self._analyses.cancel(analysis)
        await self._analytics.track(str(analysis.user_id), "analysis_cancelled")

    async def reset_conversation(self, analysis: Analysis) -> None:
        """Erase submitted context and return an owned unfinished draft to intake start."""
        if analysis.status != "draft" or analysis.intake_step == "complete":
            raise InvalidTransition("Only an unfinished active draft can be reset")
        if (
            analysis.intake_step == "waiting_for_conversation"
            and analysis.normalized_conversation_json is None
        ):
            return
        analysis.normalized_conversation_json = None
        analysis.participants_json = None
        analysis.user_participant_label = None
        analysis.user_goal = None
        analysis.relationship_stage = None
        analysis.message_count = 0
        analysis.character_count = 0
        analysis.intake_step = "waiting_for_conversation"
        await self._analyses.save(analysis)
