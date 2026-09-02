# app/db/repositories/memory_repo.py
"""
Repository for ConversationMemory — the durable, project-scoped distillation
of a conversation (Phase 3). One row per project; upsert on write.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConversationMemory


class ConversationMemoryRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_project(self, project_id: str) -> Optional[ConversationMemory]:
        result = await self.db.execute(
            select(ConversationMemory).where(ConversationMemory.project_id == project_id)
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        project_id: str,
        *,
        rolling_summary: Optional[str] = None,
        requirement_summary: Optional[str] = None,
        key_decisions: Optional[list] = None,
        rejected_options: Optional[list] = None,
        facts: Optional[dict] = None,
        understood_aspects: Optional[dict] = None,
        last_blueprint: Optional[dict] = None,
        last_checkpoint: Optional[str] = None,
    ) -> ConversationMemory:
        row = await self.get_by_project(project_id)
        values = {
            "rolling_summary": rolling_summary,
            "requirement_summary": requirement_summary,
            "key_decisions": key_decisions or [],
            "rejected_options": rejected_options or [],
            "facts": facts or {},
            "understood_aspects": understood_aspects or {},
            "last_blueprint": last_blueprint,
            "last_checkpoint": last_checkpoint,
        }
        if row is None:
            row = ConversationMemory(project_id=project_id, **values)
            self.db.add(row)
        else:
            for k, v in values.items():
                setattr(row, k, v)
        await self.db.flush()
        return row
