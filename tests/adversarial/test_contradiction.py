# tests/adversarial/test_contradiction.py
"""
Phase 3 — deterministic contradiction detection in the output gate.

A response that contradicts what the session already established (domain,
table count, GST flag) is replaced with an in-persona "let me re-check"
line rather than shown. No LLM check yet (that's Phase 4).
"""

import pytest

from app.guardrails.output_gate import check_and_repair
from app.prompts import persona
from app.engine.conversation_engine import ConversationState, ProjectBlueprint


def _state(*, domain=None, blueprint=None):
    s = ConversationState(session_id="c1")
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


# ── domain flip ─────────────────────────────────────────────

def test_domain_flip_is_replaced():
    st = _state(domain="healthcare")
    r = check_and_repair("Sounds good — this is an e-commerce platform, so we'll start with a catalog.", st)
    assert r.action == "replaced"
    assert any("contradiction:domain" in x for x in r.reasons)
    assert r.message == persona.fallback("recheck")


def test_domain_consistent_passes():
    st = _state(domain="e_commerce")
    r = check_and_repair("Right, this is an e-commerce platform — catalog, cart, orders.", st)
    assert r.action == "pass"


def test_domain_flip_ignored_when_domain_is_general():
    st = _state(domain="general")
    r = check_and_repair("this is a healthcare system with patient records", st)
    assert r.action == "pass"


def test_unknown_claimed_domain_does_not_trip():
    st = _state(domain="e_commerce")
    # "widget" is not a real domain key — not a contradiction, just prose
    r = check_and_repair("this is a widget tracking system", st)
    assert r.action == "pass"


# ── table-count flip ───────────────────────────────────────

def test_table_count_flip_is_replaced():
    st = _state(blueprint=_bp(tables=40))
    r = check_and_repair("All set — this gives you about 12 tables in total.", st)
    assert r.action == "replaced"
    assert any("contradiction:table_count" in x for x in r.reasons)


def test_table_count_within_tolerance_passes():
    st = _state(blueprint=_bp(tables=40))
    r = check_and_repair("That's ~37 tables across the modules.", st)
    assert r.action == "pass"


def test_table_count_ignored_without_a_blueprint():
    st = _state(domain="e_commerce")           # no blueprint yet
    r = check_and_repair("we might end up with 3 tables or 300 tables", st)
    assert r.action == "pass"


# ── compliance-flag (GST) flip ─────────────────────────────

def test_gst_required_then_dropped_is_replaced():
    st = _state(blueprint=_bp(gst=True))
    r = check_and_repair("Good news — no GST needed here, so the invoice table stays simple.", st)
    assert r.action == "replaced"
    assert any("contradiction:gst" in x for x in r.reasons)


def test_gst_not_required_then_added_is_replaced():
    st = _state(blueprint=_bp(gst=False))
    r = check_and_repair("I've added full GST compliance on every invoice table.", st)
    assert r.action == "replaced"
    assert any("contradiction:gst" in x for x in r.reasons)


def test_gst_consistent_passes():
    st = _state(blueprint=_bp(gst=True))
    r = check_and_repair("Every invoice table has the GST columns you need.", st)
    assert r.action == "pass"


# ── no state → no contradiction machinery ─────────────────

def test_no_state_means_no_contradiction_check():
    r = check_and_repair("this is a healthcare system with 3 tables and no GST", None)
    assert r.action == "pass"


def test_contradiction_beats_redaction_priority():
    # a message that both leaks an identifier AND contradicts — contradiction wins
    st = _state(domain="logistics")
    r = check_and_repair("Per RULE 7, this is an e-commerce platform.", st)
    assert r.action == "replaced"
    assert any("contradiction:domain" in x for x in r.reasons)
