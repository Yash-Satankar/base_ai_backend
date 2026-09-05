"""
Tests for the schema-decomposition additions to the L1-L8 abstraction
pipeline (app/engine/abstraction_pipeline.py) — see
docs/enterprise_standards_spec.md §2.2/§2.4.

compile_l4_to_l5_l6_l7 gains an optional decomposition_requested flag that
adds a coupling-classification instruction to the L7 module-compile prompt.
The default (False) path must be byte-identical to the pre-decomposition
prompt — no source supports auto-deciding decomposition, so this must stay
strictly opt-in (see the trigger tests in test_decomposition_signals.py).
"""

import json

import pytest

from app.engine import abstraction_pipeline as ap
from app.schemas.abstraction_schemas import L1_UnderstandingSpec, L4_EntitySpec, EntityItem


def _l1() -> L1_UnderstandingSpec:
    return L1_UnderstandingSpec(
        project_name="Test Project",
        business_goal="Do the thing",
        target_users=["Admin"],
        domain="logistics",
    )


def _l4() -> L4_EntitySpec:
    return L4_EntitySpec(entities=[
        EntityItem(name="Order", description="An order", entity_type="master"),
        EntityItem(name="Customer", description="A customer", entity_type="master"),
    ])


_STUB_RESPONSE = {
    "content": json.dumps({
        "relationships": [],
        "lifecycles": [],
        "modules": [
            {"name": "Orders", "description": "Order module", "entities": ["Order"],
             "workflows": [], "dependencies": ["Customers"],
             "dependency_coupling": {"Customers": "loose"}},
            {"name": "Customers", "description": "Customer module", "entities": ["Customer"],
             "workflows": [], "dependencies": []},
        ],
    }),
    "provider": "test", "model": "test", "usage": {"input_tokens": 10, "output_tokens": 10},
}


@pytest.fixture
def captured_prompt(monkeypatch):
    calls = []

    def fake_generate_schema(system_prompt, user_prompt, max_tokens=None):
        calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        return _STUB_RESPONSE

    monkeypatch.setattr(ap, "generate_schema", fake_generate_schema)
    return calls


def test_default_path_prompt_is_unchanged_by_decomposition_machinery(captured_prompt):
    """The default (no decomposition_requested arg at all) must produce the
    exact same system prompt as the pre-decomposition function — the
    single-schema path is unaffected by this feature existing."""
    ap.compile_l4_to_l5_l6_l7(_l1(), _l4())
    prompt = captured_prompt[0]["system_prompt"]
    assert "dependency_coupling" not in prompt
    assert "Schema decomposition is under consideration" not in prompt
    assert prompt.endswith('"dependencies": ["OtherModuleName"]\n    }\n  ]\n}')


def test_decomposition_requested_false_is_identical_to_default(captured_prompt):
    ap.compile_l4_to_l5_l6_l7(_l1(), _l4(), decomposition_requested=False)
    prompt_explicit_false = captured_prompt[0]["system_prompt"]

    captured_prompt.clear()
    ap.compile_l4_to_l5_l6_l7(_l1(), _l4())
    prompt_default = captured_prompt[0]["system_prompt"]

    assert prompt_explicit_false == prompt_default


def test_decomposition_requested_true_adds_coupling_instruction(captured_prompt):
    ap.compile_l4_to_l5_l6_l7(_l1(), _l4(), decomposition_requested=True)
    prompt = captured_prompt[0]["system_prompt"]
    assert "dependency_coupling" in prompt
    assert "tight" in prompt and "loose" in prompt
    assert "Schema decomposition is under consideration" in prompt


def test_module_item_parses_dependency_coupling_when_present(captured_prompt):
    _, _, m_spec = ap.compile_l4_to_l5_l6_l7(_l1(), _l4(), decomposition_requested=True)
    orders = next(m for m in m_spec.modules if m.name == "Orders")
    assert orders.dependency_coupling == {"Customers": "loose"}
    customers = next(m for m in m_spec.modules if m.name == "Customers")
    assert customers.dependency_coupling == {}
