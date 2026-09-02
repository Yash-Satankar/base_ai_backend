# tests/adversarial/test_output_repair.py
"""
Phase 4 — one LLM repair attempt in the output gate.

A structural / incoherent / contradiction catch gets ONE tightened,
temperature-0 regeneration before the canned fallback. Leakage is never
sent for an LLM rewrite — it stays deterministic redaction.
"""

import pytest

from app.guardrails import output_gate
from app.guardrails.output_gate import check_and_repair, guard_text
from app.prompts import persona
from app.engine.conversation_engine import ConversationState, ProjectBlueprint


def _state(*, domain=None, blueprint=None):
    s = ConversationState(session_id="r1")
    if domain:
        s.facts["_domain"] = domain
    s.blueprint = blueprint
    return s


def _bp(*, tables=40, gst=False):
    return ProjectBlueprint(
        project_name="Acme", description="d", domain="e_commerce", all_domains=["e_commerce"],
        modules=[{"name": "M", "description": "m",
                  "tables": [{"name": f"t{i}", "purpose": "p"} for i in range(tables)]}],
        rules_to_apply=[], scale="medium", gst_required=gst,
    )


@pytest.fixture
def stub_repair(monkeypatch):
    """Patch the LLM call used by _try_llm_repair. `calls` records kwargs;
    `reply` (mutable) is what the fake returns as content."""
    calls = []
    box = {"reply": "Here's a clean version of that — your catalog, orders and customers."}

    def fake_call_llm(**kw):
        calls.append(kw)
        return {"content": box["reply"], "usage": {"input_tokens": 50, "output_tokens": 30},
                "model": "llama-3.3-70b-versatile", "operation": kw.get("operation"),
                "cost_usd": 0.0, "degraded": False, "cached": False}

    import app.conversation.llm_client as _lc
    monkeypatch.setattr(_lc, "call_llm", fake_call_llm)
    return calls, box


# ── happy path: repair succeeds ──────────────────────────────

def test_repair_fixes_a_contradiction(stub_repair):
    calls, _ = stub_repair
    st = _state(domain="healthcare")
    r = check_and_repair("Great, this is an e-commerce platform, so we'll start with a catalog.",
                         st, allow_llm_repair=True)
    assert r.action == "repaired"
    assert r.message.startswith("Here's a clean version")
    assert any("contradiction:domain" in x for x in r.reasons)
    assert len(calls) == 1


def test_repair_fixes_broken_json(stub_repair):
    calls, _ = stub_repair
    r = check_and_repair('Here is the result: {"tables": 5, "score": 88', _state(),
                         allow_llm_repair=True)
    assert r.action == "repaired"
    assert "broken_json" in r.reasons
    assert len(calls) == 1


def test_repair_uses_temperature_zero_and_tags_the_operation(stub_repair):
    calls, _ = stub_repair
    check_and_repair('broken {"x": ', _state(), allow_llm_repair=True)
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["operation"] == "output_repair"


def test_guard_text_wrapper_triggers_repair(stub_repair):
    calls, box = stub_repair
    box["reply"] = "Sorted — a clean summary of your design."
    out = guard_text('half a thing {"a":', _state())
    assert out == "Sorted — a clean summary of your design."
    assert len(calls) == 1


# ── fallbacks when the repair can't be trusted ──────────────

def test_falls_back_when_llm_says_unrepairable(stub_repair):
    _, box = stub_repair
    box["reply"] = "UNREPAIRABLE"
    r = check_and_repair('broken {"x": ', _state(), allow_llm_repair=True)
    assert r.action == "replaced"
    assert r.message == persona.fallback("turn_error")


def test_falls_back_when_repair_still_contradicts(stub_repair):
    _, box = stub_repair
    box["reply"] = "Sure — this is an e-commerce platform with a product catalog."
    st = _state(domain="healthcare")
    r = check_and_repair("this is an e-commerce platform", st, allow_llm_repair=True)
    assert r.action == "replaced"
    assert r.message == persona.fallback("recheck")


def test_falls_back_when_llm_errors(monkeypatch):
    import app.conversation.llm_client as _lc

    def boom(**kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(_lc, "call_llm", boom)
    r = check_and_repair('broken {"x": ', _state(), allow_llm_repair=True)
    assert r.action == "replaced"


def test_only_one_repair_attempt(stub_repair):
    calls, box = stub_repair
    box["reply"] = 'still broken {"y": '        # repair output is itself broken
    r = check_and_repair('broken {"x": ', _state(), allow_llm_repair=True)
    assert r.action == "replaced"
    assert len(calls) == 1                       # the re-check does NOT retry


# ── leakage is never sent to the LLM ───────────────────────

def test_leakage_is_redacted_deterministically_never_repaired(stub_repair):
    calls, _ = stub_repair
    r = check_and_repair("I applied RULE 7 and used groq to build your schema.", _state(),
                         allow_llm_repair=True)
    assert r.action == "redacted"
    assert "rule 7" not in r.message.lower() and "groq" not in r.message.lower()
    assert len(calls) == 0                       # no LLM repair for a pure leak


def test_repair_output_is_re_scrubbed_for_leaks(stub_repair):
    _, box = stub_repair
    box["reply"] = "Fixed it — per RULE 12 your catalog is ready."   # repair leaks!
    r = check_and_repair('broken {"z": ', _state(), allow_llm_repair=True)
    assert r.action == "repaired"
    assert "rule 12" not in r.message.lower()    # deterministic redaction ran on the repair


# ── default is off (unit callers unaffected) ───────────────

def test_repair_is_off_by_default(monkeypatch):
    called = {"n": 0}
    import app.conversation.llm_client as _lc
    monkeypatch.setattr(_lc, "call_llm", lambda **kw: called.__setitem__("n", called["n"] + 1))
    r = check_and_repair('broken {"x": ', _state())        # no allow_llm_repair
    assert r.action == "replaced"
    assert called["n"] == 0
