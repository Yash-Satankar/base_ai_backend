# app/api/routes/projects.py
"""
Projects router.
Exposes REST endpoints for managing persistent projects and fetching historical schema versions.
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.repositories.project_repo import ProjectRepository
from app.schemas.project_schemas import (
    ProjectCreateRequest, ProjectResponse, ProjectDetailResponse,
    ProjectVersionResponse, VersionDetailResponse
)
from app.core.auth import get_current_user
from app.db.models import User
from app.core.security import limiter, sanitise_input

router = APIRouter()


@router.post("/", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_project(
    request: Request,
    body: ProjectCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Create a new persistent project container."""
    project_repo = ProjectRepository(db)
    
    clean_name = sanitise_input(body.name, strict=True)
    clean_desc = sanitise_input(body.description, strict=True) if body.description else None
    
    project = await project_repo.create(
        owner_id=current_user.id,
        name=clean_name,
        description=clean_desc,
        domain=body.domain
    )
    await db.commit()
    
    return ProjectResponse(
        id=project.id,
        owner_id=project.owner_id,
        name=project.name,
        description=project.description,
        domain=project.domain,
        version_count=project.version_count,
        latest_score=project.latest_score,
        latest_table_count=project.latest_table_count,
        created_at=project.created_at,
        updated_at=project.updated_at
    )


@router.get("/", response_model=List[ProjectResponse])
@limiter.limit("30/minute")
async def list_projects(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """List all active projects owned by the current authenticated user."""
    project_repo = ProjectRepository(db)
    projects = await project_repo.list_by_owner(current_user.id)
    return [
        ProjectResponse(
            id=p.id,
            owner_id=p.owner_id,
            name=p.name,
            description=p.description,
            domain=p.domain,
            version_count=p.version_count,
            latest_score=p.latest_score,
            latest_table_count=p.latest_table_count,
            created_at=p.created_at,
            updated_at=p.updated_at
        )
        for p in projects
    ]


@router.get("/{project_id}", response_model=ProjectDetailResponse)
@limiter.limit("30/minute")
async def get_project_details(
    request: Request,
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get project container details and list all historical schema versions."""
    from app.core.auth_helpers import verify_project_ownership
    project = await verify_project_ownership(db, project_id, current_user.id)
    project_repo = ProjectRepository(db)

    versions = await project_repo.list_versions(project_id)
    
    return ProjectDetailResponse(
        id=project.id,
        owner_id=project.owner_id,
        name=project.name,
        description=project.description,
        domain=project.domain,
        version_count=project.version_count,
        latest_score=project.latest_score,
        latest_table_count=project.latest_table_count,
        created_at=project.created_at,
        updated_at=project.updated_at,
        versions=[
            ProjectVersionResponse(
                id=v.id,
                project_id=v.project_id,
                version_number=v.version_number,
                status=v.status.value,
                domain=v.domain,
                scale=v.scale,
                gst_required=v.gst_required,
                modules_count=v.modules_count,
                tables_count=v.tables_count,
                rules_applied_count=v.rules_applied_count,
                validation_score=v.validation_score,
                validation_grade=v.validation_grade,
                created_at=v.created_at
            )
            for v in versions
        ]
    )


@router.delete("/{project_id}", response_model=dict)
@limiter.limit("10/minute")
async def delete_project(
    request: Request,
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Deactivate and soft delete a project and its history."""
    from app.core.auth_helpers import verify_project_ownership
    project = await verify_project_ownership(db, project_id, current_user.id)
    project_repo = ProjectRepository(db)

    succeeded = await project_repo.delete_project(project_id)
    if not succeeded:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not delete project."
        )
    
    # Soft-delete cascade cleanup: purge active Redis sessions linked to this project
    from sqlalchemy import select
    from app.db.models import Conversation
    from app.db.session_store import delete_session
    conv_result = await db.execute(
        select(Conversation.redis_session_id).where(Conversation.project_id == project_id)
    )
    redis_session_ids = conv_result.scalars().all()
    for sid in redis_session_ids:
        if sid:
            delete_session(sid)

    await db.commit()
    return {"success": True, "message": "Project soft deleted successfully."}


@router.get("/{project_id}/versions/{version_number}", response_model=VersionDetailResponse)
@limiter.limit("30/minute")
async def get_version_details(
    request: Request,
    project_id: str,
    version_number: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Fetch complete metadata, approved blueprint, generated DDL, and reports for a specific version."""
    from app.core.auth_helpers import verify_project_ownership
    project = await verify_project_ownership(db, project_id, current_user.id)
    project_repo = ProjectRepository(db)

    version = await project_repo.get_version_by_number(project_id, version_number)
    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version {version_number} not found for this project."
        )

    return VersionDetailResponse(
        id=version.id,
        project_id=version.project_id,
        version_number=version.version_number,
        status=version.status.value,
        domain=version.domain,
        scale=version.scale,
        gst_required=version.gst_required,
        modules_count=version.modules_count,
        tables_count=version.tables_count,
        rules_applied_count=version.rules_applied_count,
        validation_score=version.validation_score,
        validation_grade=version.validation_grade,
        created_at=version.created_at,
        blueprint=version.blueprint,
        schema_sql=version.schema_sql,
        validation_report=version.validation_report,
        sql_file_path=version.sql_file_path,
        pdf_file_path=version.pdf_file_path,
        generation_error=version.generation_error
    )
