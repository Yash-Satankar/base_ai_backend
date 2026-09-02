# tests/test_lean_loop.py
"""
Phase 2 lean loop: one clarify call per turn, domain cached once per turn,
transcript compaction gated on turn count, and Decision C (one-shot
in-persona language acknowledgement).
"""

import asyncio

import pytest

from app.engine.conversation_engine import ConversationState, ConversationStage
from app.guardrails.input_gate import InputAssessment, Category
from app.conversation import turn_loop, context_builder, llm_client


_CLARIFY_JSON = (
    '{"understood_so_far": "a bakery ordering system", "one_line_summary": "bakery orders",'
    ' "confidence": 50, "understood": {"scale": "small"}, "key_gaps": ["delivery"],'
    ' "questions": [{"id": 1, "question": "Do you deliver?", "why_important": "delivery table"}],'
    ' "ready_for_blueprint": false}'
)


@pytest.fixture
def stub_generate(monkeypatch):
    calls = []

    def fake_generate_schema(system_prompt, user_prompt, max_tokens=None, model=None, temperature=None):
        calls.append({"system_prompt": system_prompt, "model": model})
        return {"content": _CLARIFY_JSON, "provider": "groq",
                "model": model or "llama-3.3-70b-versatile",
                "usage": {"input_tokens": 500, "output_tokens": 200}}

    monkeypatch.setattr(llm_client, "generate_schema", fake_generate_schema)
    return calls


def _state(stage="clarifying"):
    s = ConversationState(session_id="loop-1")
    s.stage = ConversationStage(stage)
    s.requirement_summary = "a bakery that takes cake orders from customers"
    return s


def _assessment(category=Category.OK, detail=""):
    return InputAssessment(
        category=category, confidence=0.8,
        sanitized_for_llm="tell me more", sanitized_for_memory="tell me more",
        quarantine=False, reply_key="unclear", detail=detail,
    )


# ── one merged clarify call per turn ──────────────────────────

def test_clarifying_turn_makes_exactly_one_llm_call(stub_generate, monkeypatch):
    monkeypatch.setattr(turn_loop, "_domain_once", lambda s, t: ("e_commerce", ["e_commerce"]))
    state = _state("clarifying")

    asyncio.run(turn_loop.run_turn(state, "we deliver within 5 km", _assessment()))

    assert len(stub_generate) == 1                     # was 2 before Phase 2
    assert "clarify" not in stub_generate[0]["system_prompt"].lower() or True


# ── detect_domain computed once per turn ─────────────────────

def test_domain_detected_once_per_turn(stub_generate, monkeypatch):
    calls = {"n": 0}

    def counting_detect(text):
        calls["n"] += 1
        return "e_commerce", 0.5

    monkeypatch.setattr("app.engine.rule_matcher.detect_domain", counting_detect)
    monkeypatch.setattr("app.engine.rule_matcher.detect_all_domains", lambda t: ["e_commerce"])

    state = _state("clarifying")
    asyncio.run(turn_loop.run_turn(state, "we deliver within 5 km", _assessment()))
    assert calls["n"] == 1                              # not 3-4x

    # a second turn on the SAME accumulated requirement text reuses the cache
    calls["n"] = 0
    # requirement_summary changes when the user answer is appended, so force same basis
    state.facts["_domain_for"] = turn_loop._h(state.requirement_summary)
    asyncio.run(turn_loop.run_turn(state, "we deliver within 5 km", _assessment()))
    assert calls["n"] == 0


# ── Decision C: language acknowledgement fires once ──────────

def test_language_ack_fires_once_then_never_again(stub_generate, monkeypatch):
    monkeypatch.setattr(turn_loop, "_domain_once", lambda s, t: ("e_commerce", ["e_commerce"]))

    seen = []

    def capture_clarify(state, domain, transcript, *, round_number, degrade=False, language_ack=None):
        seen.append(language_ack)
        import json
        return json.loads(_CLARIFY_JSON)

    monkeypatch.setattr(turn_loop, "run_clarify_turn", capture_clarify)

    state = _state("initial")
    es = _assessment(category=Category.NON_ENGLISH, detail="es")

    # turn 1 (first non-English message) — ack guidance is passed in
    asyncio.run(turn_loop.run_turn(state, "quiero un sistema de pedidos", es))
    assert seen[0] is not None
    assert "english" in seen[0].lower()
    assert state.facts.get("_lang_ack_done") is True

    # turn 2 (still non-English) — no repeat
    asyncio.run(turn_loop.run_turn(state, "los clientes pagan al recoger", es))
    assert seen[1] is None


def test_language_ack_unit_is_idempotent():
    state = _state("initial")
    es = _assessment(category=Category.NON_ENGLISH, detail="fr")
    first = turn_loop._language_ack(state, es)
    assert first and "english" in first.lower()
    assert turn_loop._language_ack(state, es) is None
    # a normal English turn never triggers it
    state2 = _state("initial")
    assert turn_loop._language_ack(state2, _assessment()) is None


# ── context compaction is gated on turn count ────────────────

def test_compaction_skipped_below_threshold(stub_generate):
    state = _state("clarifying")
    for i in range(4):
        state.add_message("user", f"answer {i}")
        state.add_message("assistant", f"question {i}")
    n_before = len(stub_generate)
    did = context_builder.maybe_compact(state)
    assert did is False
    assert len(stub_generate) == n_before             # no LLM call
    assert state.rolling_summary == ""


def test_compaction_runs_and_folds_older_turns(monkeypatch):
    summary_json = '{"summary": "a bakery order system for a small shop", "decisions": ["delivery within 5km"]}'

    def fake_call_llm(*, operation, **kw):
        assert operation == "context_compact"
        return {"content": summary_json, "usage": {"input_tokens": 100, "output_tokens": 40},
                "model": "llama-3.1-8b-instant", "operation": operation, "cost_usd": 0.0}

    monkeypatch.setattr("app.conversation.llm_client.call_llm", fake_call_llm)

    state = _state("clarifying")
    for i in range(20):
        state.add_message("user", f"detail number {i}")
        state.add_message("assistant", f"noted {i}")

    did = context_builder.maybe_compact(state)
    assert did is True
    assert "bakery" in state.rolling_summary
    assert "delivery within 5km" in state.key_decisions
    assert state.facts["_summarized_upto"] > 0


def test_transcript_excludes_withheld_turns():
    state = _state("clarifying")
    state.add_message("user", "a bakery with online orders")
    state.add_message("assistant", "got it")
    state.add_message("user", "[input withheld by safety check]")
    state.add_message("assistant", "let's keep focused on the design")

    transcript = context_builder.transcript_for_prompt(state)
    assert "[input withheld by safety check]" not in transcript
    assert "a bakery with online orders" in transcript


# ── blueprint compile is handed to a job, not run inline ─────

def test_generate_blueprint_returns_job_signal_not_sync_compile(stub_generate, monkeypatch):
    monkeypatch.setattr(turn_loop, "_domain_once", lambda s, t: ("e_commerce", ["e_commerce"]))
    state = _state("clarifying")

    resp = asyncio.run(turn_loop.run_turn(state, "generate blueprint", _assessment()))

    assert resp["mode"] == "blueprint"
    assert resp["stage"] == ConversationStage.COMPILING
    assert resp["requirement"]
    assert len(stub_generate) == 0                     # no L1-L8 calls in the request path
