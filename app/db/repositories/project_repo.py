# app/db/repositories/project_repo.py
"""
Repository layer for Projects, ProjectVersions, Conversations, and UsageLogs.
Handles persistence, version history, topological sorting integration, and usage tracking.
"""

from typing import Optional, List
from datetime import datetime, timezone
from sqlalchemy import select, update, func, desc
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Project, ProjectVersion, ProjectStatus, VersionStatus, Conversation, ConversationMessage, UsageLog


class ProjectRepository:
    """Handles async operations for Projects, Versions, and Chat logs in PostgreSQL."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Project Operations ────────────────────────────────────────

    async def get_by_id(self, project_id: str) -> Optional[Project]:
        result = await self.db.execute(
            select(Project)
            .where(Project.id == project_id, Project.status == ProjectStatus.ACTIVE)
        )
        return result.scalar_one_or_none()

    async def list_by_owner(self, owner_id: str) -> List[Project]:
        result = await self.db.execute(
            select(Project)
            .where(Project.owner_id == owner_id, Project.status == ProjectStatus.ACTIVE)
            .order_by(desc(Project.created_at))
        )
        return list(result.scalars().all())

    async def create(self, owner_id: str, name: str, description: Optional[str] = None, domain: Optional[str] = None) -> Project:
        project = Project(
            owner_id=owner_id,
            name=name,
            description=description,
            domain=domain,
            status=ProjectStatus.ACTIVE
        )
        self.db.add(project)
        await self.db.flush()
        return project

    async def delete_project(self, project_id: str) -> bool:
        """Soft delete a project."""
        result = await self.db.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(status=ProjectStatus.DELETED)
        )
        return result.rowcount > 0

    # ── Project Version Operations ────────────────────────────────

    async def create_version(self, project_id: str, requirement_text: str, created_by: str, blueprint: Optional[dict] = None) -> ProjectVersion:
        """
        Creates a new draft version under a project.
        Automatically increments the version_number index (concurrency safe via subquery).
        """
        # Determine next version number
        num_query = await self.db.execute(
            select(func.coalesce(func.max(ProjectVersion.version_number), 0))
            .where(ProjectVersion.project_id == project_id)
        )
        next_ver = num_query.scalar_one() + 1

        version = ProjectVersion(
            project_id=project_id,
            version_number=next_ver,
            status=VersionStatus.DRAFT,
            blueprint=blueprint,
            requirement_text=requirement_text,
            created_by=created_by
        )
        self.db.add(version)
        
        # Update project version counter
        await self.db.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(version_count=next_ver)
        )

        await self.db.flush()
        return version

    async def get_version_by_number(self, project_id: str, version_number: int) -> Optional[ProjectVersion]:
        result = await self.db.execute(
            select(ProjectVersion)
            .where(ProjectVersion.project_id == project_id, ProjectVersion.version_number == version_number)
        )
        return result.scalar_one_or_none()

    async def get_version_by_id(self, version_id: str) -> Optional[ProjectVersion]:
        result = await self.db.execute(
            select(ProjectVersion)
            .where(ProjectVersion.id == version_id)
        )
        return result.scalar_one_or_none()

    async def get_latest_version(self, project_id: str) -> Optional[ProjectVersion]:
        result = await self.db.execute(
            select(ProjectVersion)
            .where(ProjectVersion.project_id == project_id)
            .order_by(desc(ProjectVersion.version_number))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_versions(self, project_id: str) -> List[ProjectVersion]:
        result = await self.db.execute(
            select(ProjectVersion)
            .where(ProjectVersion.project_id == project_id)
            .order_by(desc(ProjectVersion.version_number))
        )
        return list(result.scalars().all())

    async def update_version_status(self, version_id: str, status: VersionStatus, error: Optional[str] = None) -> bool:
        values = {"status": status}
        if error:
            values["generation_error"] = error
        result = await self.db.execute(
            update(ProjectVersion)
            .where(ProjectVersion.id == version_id)
            .values(**values)
        )
        return result.rowcount > 0

    async def complete_version(self, version_id: str, schema_sql: str, sql_file_path: str, pdf_file_path: str, validation: dict, metadata: dict) -> bool:
        """Saves generated schema outputs, quality metrics, and updates project aggregate cache."""
        score = validation.get("score")
        tables_count = len(validation.get("tables_found", []))

        result = await self.db.execute(
            update(ProjectVersion)
            .where(ProjectVersion.id == version_id)
            .values(
                status=VersionStatus.COMPLETE,
                schema_sql=schema_sql,
                sql_file_path=sql_file_path,
                pdf_file_path=pdf_file_path,
                validation_score=score,
                validation_grade=validation.get("grade", "F"),
                validation_report=validation,
                domain=metadata.get("primary_domain"),
                scale=metadata.get("scale"),
                gst_required=metadata.get("gst_required", False),
                modules_count=metadata.get("modules_generated"),
                tables_count=tables_count,
                rules_applied_count=metadata.get("total_rules_applied"),
                token_usage=metadata.get("token_usage"),
                generation_time_sec=metadata.get("generation_time_seconds"),
                l1_understanding=metadata.get("l1_understanding"),
                l2_capabilities=metadata.get("l2_capabilities"),
                l3_workflows=metadata.get("l3_workflows"),
                l4_entities=metadata.get("l4_entities"),
                l5_relationships=metadata.get("l5_relationships"),
                l6_lifecycles=metadata.get("l6_lifecycles"),
                l7_modules=metadata.get("l7_modules"),
                traceability_graph=metadata.get("traceability_graph")
            )
        )

        # Update Project stats cache denormalization
        version = await self.get_version_by_id(version_id)
        if version:
            await self.db.execute(
                update(Project)
                .where(Project.id == version.project_id)
                .values(
                    latest_score=score,
                    latest_table_count=tables_count,
                    domain=metadata.get("primary_domain")
                )
            )

        return result.rowcount > 0

    # ── Conversation Persistence ──────────────────────────────────

    async def get_or_create_conversation(self, redis_session_id: str, project_id: Optional[str] = None, version_id: Optional[str] = None, user_id: Optional[str] = None) -> Conversation:
        """Finds or creates a persistent conversation trace linked to a Redis session."""
        result = await self.db.execute(
            select(Conversation)
            .where(Conversation.redis_session_id == redis_session_id)
            .options(selectinload(Conversation.messages))
        )
        conversation = result.scalar_one_or_none()

        if not conversation:
            conversation = Conversation(
                redis_session_id=redis_session_id,
                project_id=project_id,
                version_id=version_id,
                user_id=user_id,
                stage="INITIAL",
                message_count=0
            )
            self.db.add(conversation)
            await self.db.flush()
        
        return conversation

    async def add_message(self, conversation_id: str, role: str, content: str, metadata: Optional[dict] = None) -> ConversationMessage:
        # Determine next sequence index
        seq_query = await self.db.execute(
            select(func.coalesce(func.max(ConversationMessage.sequence), 0))
            .where(ConversationMessage.conversation_id == conversation_id)
        )
        next_seq = seq_query.scalar_one() + 1

        message = ConversationMessage(
            conversation_id=conversation_id,
            sequence=next_seq,
            role=role,
            content=content,
            msg_metadata=metadata
        )
        self.db.add(message)

        # Update message counter cache on parent
        await self.db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                message_count=next_seq,
                stage=metadata.get("stage") if metadata else None,
                last_active_at=datetime.now(timezone.utc)
            )
        )

        await self.db.flush()
        return message

    async def get_messages(self, conversation_id: str) -> List[ConversationMessage]:
        result = await self.db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.sequence)
        )
        return list(result.scalars().all())

    # ── Usage Audits ──────────────────────────────────────────────

    async def log_usage(self, user_id: Optional[str], project_id: Optional[str], version_id: Optional[str], operation: str, response: dict, success: bool = True, error_msg: Optional[str] = None) -> UsageLog:
        usage = UsageLog(
            user_id=user_id,
            project_id=project_id,
            version_id=version_id,
            operation=operation,
            ai_provider=response.get("provider") if success else None,
            ai_model=response.get("model") if success else None,
            input_tokens=response.get("usage", {}).get("input_tokens", 0) if success else 0,
            output_tokens=response.get("usage", {}).get("output_tokens", 0) if success else 0,
            duration_sec=response.get("generation_time_seconds") if success else None,
            success=success,
            error_message=error_msg
        )
        self.db.add(usage)
        await self.db.flush()
        return usage
