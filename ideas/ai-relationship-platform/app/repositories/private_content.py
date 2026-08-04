"""Single boundary for encrypted analysis source and result persistence."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Analysis, AnalysisPrivateContent, User
from app.services.sensitive_content import ContentPurpose, SensitiveContentCipher


@dataclass(frozen=True)
class AnalysisSource:
    messages: list[dict[str, object]]
    participants: dict[str, str]
    user_participant_label: str | None = None
    user_goal: str | None = None
    relationship_stage: str | None = None


class EncryptedAnalysisContentRepository:
    def __init__(
        self, session: AsyncSession, cipher: SensitiveContentCipher, retention_days: int = 30
    ) -> None:
        self.session, self.cipher = session, cipher
        self.retention_days = retention_days

    async def store_source(self, analysis_id: UUID, source: AnalysisSource) -> None:
        row = await self._row(analysis_id, create=True)
        assert row is not None
        row.source_ciphertext = self.cipher.encrypt_json(
            ContentPurpose.ANALYSIS_SOURCE, asdict(source)
        )
        row.source_format_version = 1
        row.source_delete_after = datetime.now(UTC) + timedelta(days=self.retention_days)
        row.source_deleted_at = None
        await self.session.flush()

    async def load_source(self, analysis_id: UUID, user_id: UUID) -> AnalysisSource | None:
        statement = (
            select(AnalysisPrivateContent)
            .join(Analysis)
            .join(User)
            .where(
                Analysis.id == analysis_id,
                Analysis.user_id == user_id,
                Analysis.status.notin_(("deleted", "failed")),
                User.privacy_status == "active",
            )
        )
        row = await self.session.scalar(statement)
        if row is None or row.source_ciphertext is None:
            return None
        value = self.cipher.decrypt_json(ContentPurpose.ANALYSIS_SOURCE, row.source_ciphertext)
        if not isinstance(value, dict):
            raise ValueError("invalid decrypted source shape")
        return AnalysisSource(**cast(dict[str, object], value))  # type: ignore[arg-type]

    async def store_result(self, analysis_id: UUID, result: dict[str, object]) -> bool:
        analysis = await self.session.scalar(
            select(Analysis).join(User).where(Analysis.id == analysis_id).with_for_update()
        )
        if (
            analysis is None
            or analysis.status == "deleted"
            or analysis.user.privacy_status == "deleted"
        ):
            return False
        row = await self._row(analysis_id, create=True)
        assert row is not None
        row.result_ciphertext = self.cipher.encrypt_json(ContentPurpose.ANALYSIS_RESULT, result)
        row.result_format_version = 1
        row.result_deleted_at = None
        return True

    async def load_result(self, analysis_id: UUID, user_id: UUID) -> dict[str, object] | None:
        row = await self.session.scalar(
            select(AnalysisPrivateContent)
            .join(Analysis)
            .join(User)
            .where(
                Analysis.id == analysis_id,
                Analysis.user_id == user_id,
                Analysis.status == "completed",
                User.privacy_status == "active",
            )
        )
        if row is None or row.result_ciphertext is None:
            return None
        value = self.cipher.decrypt_json(ContentPurpose.ANALYSIS_RESULT, row.result_ciphertext)
        return cast(dict[str, object], value)

    async def clear_source(self, row: AnalysisPrivateContent, now: datetime | None = None) -> None:
        row.source_ciphertext = None
        row.source_deleted_at = now or datetime.now(UTC)

    async def clear_all(self, row: AnalysisPrivateContent, now: datetime | None = None) -> None:
        timestamp = now or datetime.now(UTC)
        row.source_ciphertext = row.result_ciphertext = None
        row.source_deleted_at = row.result_deleted_at = timestamp

    async def _row(self, analysis_id: UUID, *, create: bool) -> AnalysisPrivateContent | None:
        row = await self.session.get(AnalysisPrivateContent, analysis_id)
        if row is None and create:
            row = AnalysisPrivateContent(analysis_id=analysis_id)
            self.session.add(row)
            await self.session.flush()
        return row
