# tests/test_session_codec.py
"""
Covers app/db/session_codec.py — the pickle -> versioned-JSON swap for the
Redis session store.

The critical case is `test_pickle_read_fallback`: sessions written by the old
code are raw pickle bytes, and `decode()` must still read them so live
sessions survive the deploy that ships this change.
"""

import json
import pickle

from app.engine.conversation_engine import (
    ConversationState,
    ConversationMessage,
    ProjectBlueprint,
    ConversationStage,
)
from app.db import session_codec


def _sample_state() -> ConversationState:
    s = ConversationState(session_id="sess-123")
    s.stage = ConversationStage.BLUEPRINT
    s.add_message("user", "I want an e-commerce store")
    s.add_message("assistant", "Great — tell me more about it")
    s.requirement_summary = "e-commerce store with orders and payments"
    s.clarifications_done = 2
    s.questions_asked = ["who are the users?"]
    s.understood_aspects = {"scale": "medium", "users": ["buyer", "seller"]}
    s.l1_data = {"project_name": "Shop", "domain": "e_commerce"}
    s.blueprint = ProjectBlueprint(
        project_name="Shop",
        description="online store",
        domain="e_commerce",
        all_domains=["e_commerce"],
        modules=[{"name": "Core", "tables": [], "description": "core"}],
        rules_to_apply=[1, 2, 3],
        scale="medium",
        gst_required=True,
        confirmed=True,
    )
    return s


def test_json_roundtrip_preserves_structure():
    s = _sample_state()

    raw = session_codec.encode(s)
    assert isinstance(raw, bytes)

    # It really is our JSON envelope, not pickle.
    envelope = json.loads(raw.decode("utf-8"))
    assert envelope["__codec__"] == "conversation_state_json"
    assert envelope["__version__"] == session_codec.SCHEMA_VERSION

    out = session_codec.decode(raw)
    assert isinstance(out, ConversationState)
    assert out.session_id == "sess-123"
    assert out.stage == ConversationStage.BLUEPRINT
    assert isinstance(out.stage, ConversationStage)

    assert len(out.messages) == 2
    assert all(isinstance(m, ConversationMessage) for m in out.messages)
    assert out.messages[0].role == "user"
    assert out.messages[1].content == "Great — tell me more about it"

    assert isinstance(out.blueprint, ProjectBlueprint)
    assert out.blueprint.project_name == "Shop"
    assert out.blueprint.gst_required is True
    assert out.blueprint.rules_to_apply == [1, 2, 3]

    assert out.requirement_summary == s.requirement_summary
    assert out.clarifications_done == 2
    assert out.understood_aspects == s.understood_aspects
    assert out.l1_data == {"project_name": "Shop", "domain": "e_commerce"}


def test_pickle_read_fallback():
    """Legacy sessions were stored as pickled ConversationState objects.
    decode() must transparently read them (then they get rewritten as JSON
    on the next save)."""
    s = _sample_state()
    legacy_bytes = pickle.dumps(s)

    out = session_codec.decode(legacy_bytes)

    assert isinstance(out, ConversationState)
    assert out.session_id == "sess-123"
    assert out.stage == ConversationStage.BLUEPRINT
    assert len(out.messages) == 2
    assert isinstance(out.blueprint, ProjectBlueprint)
    assert out.blueprint.project_name == "Shop"
    assert out.blueprint.confirmed is True


def test_pickle_fallback_then_reencode_as_json():
    """After a legacy read, the state must re-encode cleanly as JSON so the
    session is upgraded in place."""
    legacy_bytes = pickle.dumps(_sample_state())
    recovered = session_codec.decode(legacy_bytes)

    reencoded = session_codec.encode(recovered)
    envelope = json.loads(reencoded.decode("utf-8"))
    assert envelope["__codec__"] == "conversation_state_json"

    final = session_codec.decode(reencoded)
    assert final.session_id == "sess-123"
    assert final.stage == ConversationStage.BLUEPRINT


def test_decode_garbage_returns_none():
    assert session_codec.decode(b"\x00\x01\x02 not valid at all") is None
    assert session_codec.decode(b"") is None
    assert session_codec.decode(None) is None


def test_decode_non_envelope_json_returns_none():
    # Valid JSON, but not our envelope and not pickle -> unusable.
    assert session_codec.decode(b'{"hello": "world"}') is None


def test_unknown_fields_are_dropped_on_load():
    """A field removed in a future version must not blow up an old session."""
    s = _sample_state()
    payload = json.loads(session_codec.encode(s).decode("utf-8"))
    payload["data"]["some_removed_field"] = 999
    raw = json.dumps(payload).encode("utf-8")

    out = session_codec.decode(raw)
    assert isinstance(out, ConversationState)
    assert not hasattr(out, "some_removed_field")
    assert out.session_id == "sess-123"


def test_missing_new_field_falls_back_to_default():
    """A field added in a future version is absent from an old payload and
    must fall back to the dataclass default."""
    s = _sample_state()
    payload = json.loads(session_codec.encode(s).decode("utf-8"))
    payload["data"].pop("fix_attempts", None)
    payload["data"].pop("l7_data", None)
    raw = json.dumps(payload).encode("utf-8")

    out = session_codec.decode(raw)
    assert out.fix_attempts == 0
    assert out.l7_data is None


def test_roundtrip_with_no_blueprint_and_initial_stage():
    s = ConversationState(session_id="fresh")
    s.add_message("user", "hello")

    out = session_codec.decode(session_codec.encode(s))
    assert out.stage == ConversationStage.INITIAL
    assert out.blueprint is None
    assert len(out.messages) == 1
