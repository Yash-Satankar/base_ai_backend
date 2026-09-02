# app/api/routes/conversation.py

import logging
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.encoders import jsonable_encoder
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
from app.core.debug_gate import require_debug_view
from app.prompts.persona import fallback as persona_fallback
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.database import get_db
from app.core.auth import get_current_user_optional
from app.db.models import User

# HTTP status codes that carry meaning for the client and must NOT be
# swallowed into an in-persona turn (auth, rate-limit, not-found, bad-request).
_CLIENT_ERROR_CODES = {400, 401, 403, 404, 422, 429}

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
    # Lean, default conversational contract. Internal detail (L1-L7 metadata,
    # rule IDs, validator breakdown, provider/model names) is intentionally
    # NOT here — it is only served on the debug projection (see require_debug_view).
    session_id: str
    message: str
    stage: str
    detected_domain: Optional[str] = None
    blueprint: Optional[dict] = None
    schema_sql: Optional[str] = Field(None, alias="schema")
    ready_to_generate: bool = False
    requirement: Optional[str] = None
    additional_context: Optional[str] = None
    download_urls: Optional[dict] = None
    mode: Optional[str] = None          # "blueprint" when the frontend should run the compile job

    model_config = ConfigDict(populate_by_name=True)


# Keys produced by the conversation engine that carry internal architecture
# detail. Stripped from the default response; passed through only for a
# staff `X-Debug: true` request.
_DEBUG_ONLY_KEYS = {"metadata", "validation"}


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
    debug_view: bool = Depends(require_debug_view),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """
    Send a message to the active session and get the next response.
    Triggers intent detection, clarification, or blueprint generation.

    A conversational turn never fails with a 5xx: if anything downstream
    breaks, the assistant answers in-persona and the stage is preserved so
    the user can simply continue. Only genuine client-side conditions
    (bad request, auth, not-found, rate limit) are surfaced as error codes.
    """
    # Sanitise input
    clean_message = sanitise_input(body.message)

    state = get_session(body.session_id)
    if not state:
        raise HTTPException(
            status_code=404,
            detail="Session not found. Please start a new session.",
        )

    response = None
    try:
        response = await process_message(body.session_id, clean_message, db, current_user)
    except HTTPException as he:
        if he.status_code in _CLIENT_ERROR_CODES:
            raise
        logger.error(
            f"Conversation turn failed ({he.status_code}): {he.detail}",
            exc_info=True,
        )
    except Exception as e:
        logger.error(f"Conversation turn failed: {e}", exc_info=True)

    if response is None:
        latest = get_session(body.session_id) or state
        stage_val = (
            latest.stage.value if latest and latest.stage else "unknown"
        )
        return MessageResponse(
            session_id=body.session_id,
            message=persona_fallback("turn_error", stage=stage_val),
            stage=stage_val,
        )

    if debug_view:
        # Staff-only: pass the full engine payload through untouched.
        return JSONResponse(content=jsonable_encoder(response))

    lean = {k: v for k, v in response.items() if k not in _DEBUG_ONLY_KEYS}
    return MessageResponse(**lean)


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