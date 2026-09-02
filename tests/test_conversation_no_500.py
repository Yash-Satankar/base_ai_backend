# tests/test_conversation_no_500.py
"""
A conversational turn must never come back as a 5xx.

If anything downstream of the route breaks — the engine raises, an LLM call
throws a 503, Redis blows up before the try block — the user gets an
in-persona 200 with the stage preserved, not a stack-trace or an error page.
Genuine client conditions (404 for a missing session/project, 429 rate limit)
still surface as themselves.
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.db.database import get_db
from app.core.auth import get_current_user_optional
import app.api.routes.conversation as conv_route
from app.prompts.persona import fallback as persona_fallback
from app.engine.conversation_engine import ConversationState, ConversationStage


@pytest.fixture
def client():
    async def _fake_db():
        yield None

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_user_optional] = lambda: None
    # raise_server_exceptions=False so the registered Exception handler is
    # exercised instead of the exception bubbling into the test.
    c = TestClient(app, raise_server_exceptions=False)
    try:
        yield c
    finally:
        app.dependency_overrides.clear()


def _fake_state(stage=ConversationStage.CLARIFYING):
    s = ConversationState(session_id="sess-abc")
    s.stage = stage
    return s


_MSG = {"session_id": "sess-abc", "message": "build me a store for shoes"}


def test_turn_never_500_when_engine_raises(client, monkeypatch):
    monkeypatch.setattr(conv_route, "get_session", lambda sid: _fake_state())

    async def _boom(*a, **k):
        raise RuntimeError("kaboom deep in the pipeline")

    monkeypatch.setattr(conv_route, "process_message", _boom)

    r = client.post("/conversation/message", json=_MSG)

    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "clarifying"          # stage preserved
    assert body["message"] == persona_fallback("turn_error")
    assert "500" not in body["message"]
    assert "exception" not in body["message"].lower()


def test_turn_never_500_on_downstream_httpexception(client, monkeypatch):
    monkeypatch.setattr(conv_route, "get_session", lambda sid: _fake_state())

    async def _svc_down(*a, **k):
        raise HTTPException(
            status_code=503,
            detail="AI service unavailable across all models: groq rate limited",
        )

    monkeypatch.setattr(conv_route, "process_message", _svc_down)

    r = client.post("/conversation/message", json=_MSG)

    assert r.status_code == 200
    # Internal/provider detail from the exception must not leak into the reply.
    assert "groq" not in r.json()["message"].lower()


def test_turn_never_500_when_get_session_raises_before_try(client, monkeypatch):
    # get_session runs before the route's own try/except — this exercises the
    # route-aware global exception handler in app/main.py.
    def _explode(sid):
        raise RuntimeError("redis connection dropped before the try block")

    monkeypatch.setattr(conv_route, "get_session", _explode)

    r = client.post("/conversation/message", json=_MSG)

    assert r.status_code == 200
    assert r.json().get("message")


def test_client_errors_still_surface(client, monkeypatch):
    monkeypatch.setattr(conv_route, "get_session", lambda sid: _fake_state())

    async def _project_gone(*a, **k):
        raise HTTPException(status_code=404, detail="Project not found")

    monkeypatch.setattr(conv_route, "process_message", _project_gone)

    r = client.post("/conversation/message", json=_MSG)
    assert r.status_code == 404


def test_missing_session_still_404(client, monkeypatch):
    monkeypatch.setattr(conv_route, "get_session", lambda sid: None)

    r = client.post("/conversation/message", json=_MSG)
    assert r.status_code == 404


def test_happy_path_is_lean_by_default(client, monkeypatch):
    monkeypatch.setattr(conv_route, "get_session", lambda sid: _fake_state())

    async def _ok(*a, **k):
        return {
            "session_id": "sess-abc",
            "message": "Here's what I've got so far.",
            "stage": "clarifying",
            "metadata": {"ai_provider": "groq", "l1_understanding": {"x": 1}},
            "validation": {"score": 88, "issues": [{"rule_id": 7}]},
        }

    monkeypatch.setattr(conv_route, "process_message", _ok)

    r = client.post("/conversation/message", json=_MSG)
    assert r.status_code == 200
    body = r.json()
    # Internal detail stripped from the default contract.
    assert "metadata" not in body
    assert "validation" not in body
    assert body["message"] == "Here's what I've got so far."
