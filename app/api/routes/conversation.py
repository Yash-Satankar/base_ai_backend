# app/api/routes/conversation.py

import logging
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import FileResponse
import os
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional
from app.services.conversation_service import (
    create_session,
    get_session,
    process_message,
    delete_session,
)
from app.core.security import (
    limiter,
    verify_api_key,
    sanitise_input,
)
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.database import get_db
from app.core.auth import get_current_user_optional
from app.db.models import User

logger = logging.getLogger(__name__)
router = APIRouter()


class StartSessionResponse(BaseModel):
    session_id: str
    message: str
    stage: str


class SessionStatusResponse(BaseModel):
    session_id: str
    stage: str
    messages_count: int
    has_blueprint: bool
    has_schema: bool
    validation_score: Optional[int] = None
    fix_attempts: int


class DeleteSessionResponse(BaseModel):
    message: str
    session_id: str


class MessageRequest(BaseModel):
    session_id: str
    message: str


class MessageResponse(BaseModel):
    session_id: str
    message: str
    stage: str
    detected_domain: Optional[str] = None
    blueprint: Optional[dict] = None
    schema_sql: Optional[str] = Field(None, alias="schema")
    validation: Optional[dict] = None
    metadata: Optional[dict] = None
    ready_to_generate: bool = False
    requirement: Optional[str] = None
    additional_context: Optional[str] = None
    download_urls: Optional[dict] = None

    model_config = ConfigDict(populate_by_name=True)


@router.post("/start", response_model=StartSessionResponse)
@limiter.limit("10/hour")          # max 10 new sessions per hour per IP
async def start_conversation(
    request: Request,
    project_id: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """
    Start a new conversational design session.
    Optionally associates the session with a persistent project.
    """
    if project_id and current_user and db:
        from app.core.auth_helpers import verify_project_ownership
        await verify_project_ownership(db, project_id, current_user.id)

    state = await create_session(db, current_user, project_id)
    return StartSessionResponse(
        session_id=state.session_id,
        message="Hello! Tell me about the database you want to build.",
        stage=state.stage,
    )


@router.post("/message", response_model=MessageResponse)
@limiter.limit("30/minute")        # 30 messages per minute per IP
async def send_message(
    request: Request,
    body: MessageRequest,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """
    Send a message to the active session and get the next response.
    Triggers intent detection, clarification, or blueprint generation.
    """
    # Sanitise input
    clean_message = sanitise_input(body.message)

    state = get_session(body.session_id)
    if not state:
        raise HTTPException(
            status_code=404,
            detail="Session not found. Please start a new session.",
        )

    try:
        response = await process_message(body.session_id, clean_message, db, current_user)
        return MessageResponse(
            **response,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Message processing failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to process message. Please try again.",
        )


@router.get("/session/{session_id}", response_model=SessionStatusResponse)
@limiter.limit("60/minute")
async def get_session_status(
    request: Request,
    session_id: str
):
    """Get current status, stage, and metadata of a session."""
    state = get_session(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="Session not found")

    return SessionStatusResponse(
        session_id=session_id,
        stage=state.stage,
        messages_count=len(state.messages),
        has_blueprint=state.blueprint is not None,
        has_schema=state.schema is not None,
        validation_score=state.validation_score,
        fix_attempts=state.fix_attempts,
    )

@router.get("/download/sql/{session_id}")
@limiter.limit("10/minute")
async def download_sql(
    request: Request,
    session_id: str
):
    """Download the generated .sql file."""
    state = get_session(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="Session not found")
    if not state.sql_file_path or not os.path.exists(state.sql_file_path):
        raise HTTPException(status_code=404, detail="SQL file not generated yet")

    return FileResponse(
        path=state.sql_file_path,
        media_type="application/sql",
        filename=os.path.basename(state.sql_file_path),
    )


@router.get("/download/pdf/{session_id}")
@limiter.limit("10/minute")
async def download_pdf(
    request: Request,
    session_id: str
):
    """Download the generated .pdf documentation."""
    state = get_session(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="Session not found")
    if not state.pdf_file_path or not os.path.exists(state.pdf_file_path):
        raise HTTPException(status_code=404, detail="PDF not generated yet")

    return FileResponse(
        path=state.pdf_file_path,
        media_type="application/pdf",
        filename=os.path.basename(state.pdf_file_path),
    )


@router.delete("/session/{session_id}", response_model=DeleteSessionResponse)
@limiter.limit("10/minute")
async def end_session(
    request: Request,
    session_id: str
):
    """Delete a session when done."""
    delete_session(session_id)
    return DeleteSessionResponse(message="Session ended", session_id=session_id)