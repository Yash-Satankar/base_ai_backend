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

logger = logging.getLogger(__name__)
router = APIRouter()


class StartSessionResponse(BaseModel):
    session_id: str
    message: str
    stage: str


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

    model_config = ConfigDict(populate_by_name=True)


@router.post("/start")
@limiter.limit("10/hour")          # max 10 new sessions per hour per IP
async def start_conversation(request: Request):
    state = create_session()
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
    # api_key: str = Depends(verify_api_key),  # ← uncomment when ready
):
    # Sanitise input
    clean_message = sanitise_input(body.message)

    state = get_session(body.session_id)
    if not state:
        raise HTTPException(
            status_code=404,
            detail="Session not found. Please start a new session.",
        )

    try:
        response = process_message(body.session_id, clean_message)
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


@router.get("/session/{session_id}")
async def get_session_status(session_id: str):
    """Get current status of a session."""
    state = get_session(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session_id,
        "stage": state.stage,
        "messages_count": len(state.messages),
        "has_blueprint": state.blueprint is not None,
        "has_schema": state.schema is not None,
        "validation_score": state.validation_score,
        "fix_attempts": state.fix_attempts,
    }

@router.get("/download/sql/{session_id}")
async def download_sql(session_id: str):
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
async def download_pdf(session_id: str):
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


@router.delete("/session/{session_id}")
async def end_session(session_id: str):
    """Delete a session when done."""
    delete_session(session_id)
    return {"message": "Session ended", "session_id": session_id}