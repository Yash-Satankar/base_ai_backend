# tests/adversarial/test_output_gate.py
"""
app.guardrails.output_gate — deterministic redaction + structural checks.

No response may reach the user carrying internal identifiers (rule IDs,
pipeline-level names, provider/model names, internal symbols, tracebacks,
raw HTTP phrasing), broken JSON/SQL, or empty/stub text.
"""

import pytest

from app.guardrails.output_gate import check_and_repair, guard_text
from app.prompts import persona


def test_clean_message_passes_through():
    msg = "Here's the plan for your bakery database: customers, orders and products, with a join table for line items."
    r = check_and_repair(msg)
    assert r.action == "pass"
    assert r.message == msg


def test_redacts_rule_ids():
    r = check_and_repair("I applied RULE 38 and rule_id 7 to keep the audit columns consistent.")
    assert r.action == "redacted"
    assert "rule 38" not in r.message.lower()
    assert "rule_id" not in r.message.lower()


def test_redacts_provider_and_model_names():
    r = check_and_repair("This schema was generated with groq running llama-3.3-70b-versatile.")
    assert "groq" not in r.message.lower()
    assert "llama-3.3" not in r.message.lower()


def test_redacts_pipeline_level_identifiers():
    r = check_and_repair("From the l1_understanding and the L4 entities I derived the tables.")
    assert "l1_understanding" not in r.message
    assert "l4 entit" not in r.message.lower()


def test_redacts_internal_symbols_and_traceback():
    msg = (
        'Something failed in process_message.\n'
        'Traceback (most recent call last):\n'
        '  File "app/services/conversation_service.py", line 210, in process_message\n'
        '    raise HTTPException(status_code=503)\n'
        'HTTPException: service unavailable'
    )
    r = check_and_repair(msg)
    assert "process_message" not in r.message
    assert "Traceback" not in r.message
    assert "HTTPException" not in r.message
    assert "conversation_service" not in r.message


def test_redacts_stage_machine_leak():
    r = check_and_repair('We are at ConversationStage.CLARIFYING right now.')
    assert "ConversationStage" not in r.message


@pytest.mark.parametrize("stub", ["", "   ", "...", "null", "TODO", "n/a", "[response]"])
def test_empty_or_stub_is_replaced_with_persona_fallback(stub):
    r = check_and_repair(stub)
    assert r.action == "replaced"
    assert r.message == persona.fallback("turn_error")


def test_unbalanced_code_fence_is_closed():
    msg = "Here is the SQL:\n```sql\nCREATE TABLE x (id INT PRIMARY KEY)"
    r = check_and_repair(msg)
    assert r.message.count("```") % 2 == 0
    assert "unbalanced_code_fence" in r.reasons


def test_broken_embedded_json_is_replaced():
    msg = 'Here is the result: {"tables": 5, "score": 88, "modules": ['
    r = check_and_repair(msg)
    assert r.action == "replaced"
    assert "broken_json" in r.reasons


def test_well_formed_json_in_prose_is_not_flagged():
    msg = 'The config looks like {"gst": true, "scale": "medium"} and that is fine.'
    r = check_and_repair(msg)
    assert r.action == "pass"


def test_reasons_are_reported_for_redaction():
    r = check_and_repair("Applied RULE 12 via groq.")
    assert r.action == "redacted"
    assert any("leaked_identifier" in reason for reason in r.reasons)


def test_guard_text_returns_plain_string():
    assert guard_text("a perfectly ordinary sentence") == "a perfectly ordinary sentence"
    assert isinstance(guard_text("used RULE 5"), str)
    assert "rule 5" not in guard_text("used RULE 5").lower()


def test_message_that_is_only_a_leak_still_yields_something_safe():
    r = check_and_repair("process_message")
    assert "process_message" not in r.message
    assert r.message.strip()
