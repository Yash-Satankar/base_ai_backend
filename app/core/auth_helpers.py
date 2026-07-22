# app/core/auth_helpers.py
"""
Centralized authorization and project status verification helpers.
Ensures reusable ownership, active status, and access control validation across all API routes.
"""

from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Project, ProjectStatus, User


async def get_project_active_or_404(db: AsyncSession, project_id: str) -> Project:
    """
    Fetch a project by ID and ensure it is not soft-deleted.
    Raises HTTP 404 if project does not exist or has status == DELETED.
    """
    result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if not project or project.status == ProjectStatus.DELETED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found or has been deleted."
        )
    return project


async def verify_project_ownership(db: AsyncSession, project_id: str, user_id: str) -> Project:
    """
    Verify that a project exists, is active, and is owned by the specified user.
    Raises HTTP 404 if not found/deleted or HTTP 403 if owned by another user.
    """
    project = await get_project_active_or_404(db, project_id)
    if project.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found or access denied."
        )
    return project
