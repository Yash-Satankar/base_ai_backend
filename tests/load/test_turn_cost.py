# tests/load/test_turn_cost.py
"""
Decision B — warn-and-degrade.

Once a conversation crosses the soft cost threshold:
  - `should_degrade()` flips (no hard stop — calls still succeed)
  - clarifying rounds route to the cheaper model
  - a single WARNING is logged
  - NOTHING about budget / cost / limits ever reaches the user's message
"""

import asyncio
import logging

import pytest

from app.conversation import llm_client
from app.core.config import settings


# ── stub the underlying provider call ───────────────────────────

_CLARIFY_JSON = (
    '{"understood_so_far": "a small shoe store with orders and customers",'
    ' "one_line_summary": "shoe store", "confidence": 55, "understood": {},'
    ' "key_gaps": ["returns"], "questions": [{"id": 1, "question": "How are returns handled?",'
    ' "why_important": "affects the returns table"}], "ready_for_blueprint": false}'
)


@pytest.fixture
def stub_generate(monkeypatch):
    calls = []

    def fake_generate_schema(system_prompt, user_prompt, max_tokens=None, model=None, temperature=None):
        calls.append({"model": model, "system_prompt": system_prompt, "user_prompt": user_prompt})
        return {
            "content": _CLARIFY_JSON,
            "provider": "groq",
            "model": model or settings.GROQ_MODEL,
            "usage": {"input_tokens": 4000, "output_tokens": 1500},
        }

    # llm_client imported generate_schema by name
    monkeypatch.setattr(llm_client, "generate_schema", fake_generate_schema)
    return calls


# ── mandatory operation tag ────────────────────────────────────

def test_operation_tag_is_mandatory(stub_generate):
    with pytest.raises(ValueError):
        llm_client.call_llm(operation="", system_prompt="s", user_prompt="u")
    out = llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u")
    assert out["operation"] == "clarify_turn"


# ── cost accounting ───────────────────────────────────────────

def test_cost_accumulates_per_conversation(stub_generate):
    sid = "conv-A"
    assert llm_client.conversation_cost(sid) == 0.0
    llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u", session_id=sid)
    after_one = llm_client.conversation_cost(sid)
    assert after_one > 0
    llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u2", session_id=sid)
    assert llm_client.conversation_cost(sid) > after_one


def test_turn_cost_is_scoped_to_the_turn(stub_generate):
    sid = "conv-B"
    llm_client.reset_turn_cost()
    llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u", session_id=sid)
    t1 = llm_client.turn_cost()
    assert t1 > 0
    llm_client.reset_turn_cost()
    assert llm_client.turn_cost() == 0.0


# ── degrade behaviour ────────────────────────────────────────

def test_degrade_trips_at_threshold_and_switches_model(stub_generate, monkeypatch):
    monkeypatch.setattr(settings, "CONVERSATION_COST_WARN_USD", 0.001)  # trivially crossable
    sid = "conv-C"

    assert llm_client.should_degrade(sid) is False
    # one big call blows past 0.001
    llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u", session_id=sid)
    assert llm_client.should_degrade(sid) is True

    stub_generate.clear()
    llm_client.call_llm(
        operation="clarify_turn", system_prompt="s", user_prompt="u2",
        session_id=sid, degrade=llm_client.should_degrade(sid),
    )
    # the provider call was made with the cheaper model forced
    assert stub_generate[-1]["model"] == settings.DEGRADE_MODEL


def test_auto_degrade_without_explicit_flag(stub_generate, monkeypatch):
    monkeypatch.setattr(settings, "CONVERSATION_COST_WARN_USD", 0.001)
    sid = "conv-D"
    llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u", session_id=sid)
    stub_generate.clear()
    # no degrade= passed — call_llm should still auto-degrade because it's over budget
    out = llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u2", session_id=sid)
    assert stub_generate[-1]["model"] == settings.DEGRADE_MODEL
    assert out["degraded"] is True


def test_no_hard_stop_ever(stub_generate, monkeypatch):
    monkeypatch.setattr(settings, "CONVERSATION_COST_WARN_USD", 0.001)
    sid = "conv-E"
    for i in range(6):
        out = llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt=f"u{i}", session_id=sid)
        assert out["content"]           # every call still returns a real response


def test_threshold_crossing_logs_one_warning(stub_generate, monkeypatch, caplog):
    monkeypatch.setattr(settings, "CONVERSATION_COST_WARN_USD", 0.001)
    sid = "conv-F"
    with caplog.at_level(logging.WARNING, logger="app.conversation.llm_client"):
        llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u", session_id=sid)
        llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u2", session_id=sid)
    warnings = [r for r in caplog.records if "soft cost threshold" in r.message]
    assert len(warnings) == 1     # warned once, not every call


# ── the degrade must be invisible to the user ─────────────────

_FORBIDDEN = ["budget", "cost", "limit", "threshold", "degrad", "cheaper",
              "expensive", "quota", "spend", "pricing", "over the", "$"]


def _mk_state(stage_name):
    from app.engine.conversation_engine import ConversationState, ConversationStage
    s = ConversationState(session_id="conv-G")
    s.stage = ConversationStage(stage_name)
    s.requirement_summary = "a small shoe store with orders and customers"
    return s


def _mk_assessment():
    from app.guardrails.input_gate import assess_input
    return assess_input("returns should go back to stock within 30 days")


def test_degraded_clarifying_turn_has_no_budget_language(stub_generate, monkeypatch):
    monkeypatch.setattr(settings, "CONVERSATION_COST_WARN_USD", 0.001)
    # keep domain detection off the network
    from app.conversation import turn_loop
    monkeypatch.setattr(turn_loop, "_domain_once", lambda state, text: ("e_commerce", ["e_commerce"]))

    state = _mk_state("clarifying")
    # push the conversation over the soft threshold
    llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u", session_id=state.session_id)
    assert llm_client.should_degrade(state.session_id) is True

    resp = asyncio.run(turn_loop.run_turn(state, "returns go back to stock in 30 days", _mk_assessment()))

    msg = resp["message"].lower()
    for word in _FORBIDDEN:
        assert word not in msg, f"user-facing message leaked cost word: {word!r}"
    # and it really did degrade under the hood
    assert stub_generate[-1]["model"] == settings.DEGRADE_MODEL


# ── Decision B — final cost-ceiling contract ──────────────────

def test_there_is_no_hard_ceiling_config(stub_generate):
    """Only a *soft* warn threshold exists — no MAX / HARD / CEILING knob."""
    fields = set(settings.model_fields) if hasattr(settings, "model_fields") else set(vars(settings))
    assert "CONVERSATION_COST_WARN_USD" in fields
    for name in fields:
        up = name.upper()
        if "COST" in up or "BUDGET" in up:
            assert not any(t in up for t in ("MAX", "HARD", "CEIL", "CAP", "LIMIT")), \
                f"unexpected hard-ceiling setting: {name}"


def test_calls_still_succeed_far_past_the_threshold(stub_generate, monkeypatch):
    monkeypatch.setattr(settings, "CONVERSATION_COST_WARN_USD", 0.001)
    sid = "conv-farover"
    for i in range(60):
        out = llm_client.call_llm(operation="clarify_turn", system_prompt="s",
                                  user_prompt=f"u{i}", session_id=sid)
        assert out["content"] and out["cached"] is False
    # dozens of calls, far past the 0.001 soft threshold, and still running
    assert llm_client.conversation_cost(sid) > 0.015


def test_should_degrade_is_a_pure_function_of_accumulated_cost(stub_generate, monkeypatch):
    monkeypatch.setattr(settings, "CONVERSATION_COST_WARN_USD", 0.005)
    sid = "conv-pure"
    assert llm_client.should_degrade(sid) is False
    while llm_client.conversation_cost(sid) < 0.005:
        llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u", session_id=sid)
    assert llm_client.should_degrade(sid) is True
    # lowering the accumulated cost view (fresh session) => not degraded
    assert llm_client.should_degrade("conv-pure-2") is False


def test_degrade_is_isolated_per_conversation(stub_generate, monkeypatch):
    monkeypatch.setattr(settings, "CONVERSATION_COST_WARN_USD", 0.001)
    hot = "conv-hot"
    llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u", session_id=hot)
    assert llm_client.should_degrade(hot) is True

    cold = "conv-cold"
    stub_generate.clear()
    out = llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u", session_id=cold)
    assert out["degraded"] is False
    assert stub_generate[-1]["model"] != settings.DEGRADE_MODEL


def test_one_warning_even_across_many_turns(stub_generate, monkeypatch, caplog):
    monkeypatch.setattr(settings, "CONVERSATION_COST_WARN_USD", 0.001)
    sid = "conv-manyturns"
    with caplog.at_level(logging.WARNING, logger="app.conversation.llm_client"):
        for i in range(12):
            llm_client.call_llm(operation="clarify_turn", system_prompt="s",
                                user_prompt=f"u{i}", session_id=sid)
    warnings = [r for r in caplog.records if "soft cost threshold" in r.message]
    assert len(warnings) == 1


def test_turn_cost_accumulates_within_a_turn_and_resets(stub_generate):
    sid = "conv-turncost"
    llm_client.reset_turn_cost()
    llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="a", session_id=sid)
    one = llm_client.turn_cost()
    llm_client.call_llm(operation="context_compact", system_prompt="s", user_prompt="b", session_id=sid)
    two = llm_client.turn_cost()
    assert two > one > 0
    llm_client.reset_turn_cost()
    assert llm_client.turn_cost() == 0.0
