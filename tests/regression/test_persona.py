# tests/regression/test_persona.py
"""
Phase 4 — persona / tone regression.

Every user-facing line — every stage's turn message, every canned fallback,
every intent-handler reply — must read as one coherent assistant:
  - no internal identifiers (reuses the leakage scanner)
  - no "As an AI" / "language model" robotic phrasing
  - no raw-error phrasing (traceback, internal server error, exception, ...)
  - a conversational pronoun (I / we / you / let's) — never third-person
    self-reference ("the assistant", "the system")
"""

import asyncio
import re

import pytest

from app.guardrails import leakage
from app.prompts import persona
from app.engine.conversation_engine import ConversationState, ConversationStage, ProjectBlueprint
from app.conversation import turn_loop

_ROBOTIC = [
    "as an ai", "as a language model", "i am an ai", "i'm just a model",
    "i'm a language model", "language model", "i cannot fulfill", "i am unable to",
    "i'm not able to comply",
]
_RAW_ERROR = [
    "internal server error", "traceback", "stack trace", "nonetype",
    "null pointer", "status_code", "http 5", "exception:", "errno",
    " undefined", "500 error",
]
_THIRD_PERSON_SELF = ["the assistant ", "the system encountered", "the bot ", "this ai "]
_PRONOUN_RE = re.compile(r"\b(i|i'?m|i'?ll|i'?ve|i'?d|my|me|we|us|our|you|your|let'?s)\b", re.I)


def _assert_in_persona(msg: str, *, where: str):
    assert msg and msg.strip(), f"{where}: empty message"
    low = msg.lower()
    assert leakage.scan(msg) == [], f"{where}: leaked internal identifier -> {leakage.scan(msg)}"
    for p in _ROBOTIC:
        assert p not in low, f"{where}: robotic phrasing {p!r}"
    for p in _RAW_ERROR:
        assert p not in low, f"{where}: raw-error phrasing {p!r}"
    for p in _THIRD_PERSON_SELF:
        assert p not in low, f"{where}: third-person self-reference {p!r}"
    assert _PRONOUN_RE.search(low), f"{where}: no conversational pronoun -> {msg!r}"


# ── every canned fallback line ──────────────────────────────

def test_all_persona_fallbacks_are_in_voice():
    keys = list(persona._FALLBACKS)
    assert keys, "no fallbacks defined"
    for k in keys:
        _assert_in_persona(persona.fallback(k), where=f"fallback[{k}]")
    # an unknown key still returns a real in-voice line
    _assert_in_persona(persona.fallback("does-not-exist"), where="fallback[unknown]")


# ── turn-loop stage messages ───────────────────────────────

_CLARIFY = {
    "understood_so_far": "a bakery that takes cake orders online",
    "one_line_summary": "bakery cake orders",
    "confidence": 55, "understood": {"scale": "small"},
    "key_gaps": ["delivery radius"],
    "questions": [{"id": 1, "question": "Do you deliver, or is it pickup only?",
                   "why_important": "decides the delivery table"}],
    "ready_for_blueprint": False,
}


@pytest.fixture
def stubbed_turn(monkeypatch):
    monkeypatch.setattr(turn_loop, "_domain_once", lambda s, t: ("e_commerce", ["e_commerce"]))
    monkeypatch.setattr(turn_loop, "run_clarify_turn",
                        lambda *a, **k: dict(_CLARIFY))
    # context compaction must not fire / call out
    monkeypatch.setattr("app.conversation.context_builder.maybe_compact", lambda state: False)


def _state(stage="initial"):
    s = ConversationState(session_id="p1")
    s.stage = ConversationStage(stage)
    s.requirement_summary = "a bakery that takes cake orders from customers online"
    return s


def _assessment():
    from app.guardrails.input_gate import InputAssessment, Category
    return InputAssessment(category=Category.OK, confidence=0.8,
                           sanitized_for_llm="tell me more", sanitized_for_memory="tell me more",
                           quarantine=False, reply_key="unclear")


def test_initial_turn_message_is_in_persona(stubbed_turn):
    resp = asyncio.run(turn_loop.run_turn(_state("initial"), "I run a small bakery", _assessment()))
    _assert_in_persona(resp["message"], where="stage:initial")


def test_clarifying_turn_message_is_in_persona(stubbed_turn):
    resp = asyncio.run(turn_loop.run_turn(_state("clarifying"), "pickup only", _assessment()))
    _assert_in_persona(resp["message"], where="stage:clarifying")


def test_blueprint_trigger_message_is_in_persona(stubbed_turn):
    resp = asyncio.run(turn_loop.run_turn(_state("clarifying"), "generate blueprint", _assessment()))
    _assert_in_persona(resp["message"], where="stage:compiling(trigger)")


def test_compiling_generating_complete_stage_messages_are_in_persona(stubbed_turn):
    for stage in ("compiling", "generating", "complete"):
        resp = asyncio.run(turn_loop.run_turn(_state(stage), "how's it going", _assessment()))
        _assert_in_persona(resp["message"], where=f"stage:{stage}")


# ── blueprint confirmation + formatted blueprint text ──────

def _blueprint():
    return ProjectBlueprint(
        project_name="Sweet Crumb", description="a bakery order system", domain="e_commerce",
        all_domains=["e_commerce"],
        modules=[{"name": "Orders", "description": "order taking",
                  "tables": [{"name": "order_header_all", "purpose": "one row per order"}]}],
        rules_to_apply=[1], scale="small", gst_required=False, confirmed=False,
    )


def test_blueprint_confirmation_message_is_in_persona():
    import app.services.conversation_service as cs
    st = _state("blueprint")
    st.blueprint = _blueprint()
    resp = cs._handle_blueprint_confirmation(st, "yes")
    _assert_in_persona(resp["message"], where="stage:confirmed")


def test_formatted_blueprint_text_is_in_persona():
    import app.services.conversation_service as cs
    text = cs._format_blueprint(_blueprint())
    # _format_blueprint is a fragment (no pronoun), so just check it stays clean
    assert leakage.scan(text) == []
    low = text.lower()
    for p in _RAW_ERROR + _ROBOTIC:
        assert p not in low


# ── intent-handler canned replies ─────────────────────────

def test_intent_handler_replies_are_in_persona():
    from app.engine import intent_handlers as ih
    from app.engine.intent_detector import Intent, IntentType

    st = _state("clarifying")
    checks = [
        ("start_over", ih.handle_start_over(st)),
        ("session_summary", ih.handle_session_summary(st)),
        ("ambiguous", ih.handle_ambiguous(st, "hm", Intent(type=IntentType.AMBIGUOUS, confidence=0.5))),
        ("regenerate_no_bp", ih.handle_regenerate(_state("clarifying"))),
        ("download_not_ready", ih.handle_download_request(_state("clarifying"),
                                                         Intent(type=IntentType.DOWNLOAD, confidence=0.9))),
    ]
    for name, resp in checks:
        _assert_in_persona(resp["message"], where=f"intent:{name}")


# ── cross-stage tone consistency ──────────────────────────

def test_tone_is_consistent_across_stages(stubbed_turn):
    msgs = []
    for stage, text in [("initial", "I run a bakery"), ("clarifying", "pickup only"),
                        ("generating", "status"), ("complete", "status")]:
        msgs.append(asyncio.run(turn_loop.run_turn(_state(stage), text, _assessment()))["message"])
    msgs += [persona.fallback(k) for k in persona._FALLBACKS]

    for m in msgs:
        low = m.lower()
        assert "as an ai" not in low and "language model" not in low
        assert "internal server error" not in low and "traceback" not in low
        assert leakage.scan(m) == []
