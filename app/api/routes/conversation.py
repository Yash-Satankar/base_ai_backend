# app/api/routes/conversation.py

from fastapi import APIRouter, HTTPException
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


@router.post("/start", response_model=StartSessionResponse)
async def start_conversation():
    """
    Start a new schema generation conversation.
    Returns a session_id to use in all subsequent messages.
    """
    state = create_session()
    return StartSessionResponse(
        session_id=state.session_id,
        message="Hello! Tell me about the database you want to build. Describe your project — what it does, who uses it, and what data it needs to manage.",
        stage=state.stage,
    )


@router.post("/message", response_model=MessageResponse)
@limiter.limit("30/minute")
async def send_message(body: MessageRequest):
    """
    Send a message in an existing conversation.
    
    Flow:
    1. First message → domain detection + clarifying questions
    2. Answer questions → blueprint shown
    3. Confirm blueprint → schema generated + validated
    4. Schema delivered with .sql and .pdf
    """
    state = get_session(body.session_id)
    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"Session '{body.session_id}' not found. Start a new session first."
        )

    try:
        response = process_message(body.session_id, body.message)
        response["session_id"] = body.session_id
        return MessageResponse(**response)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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