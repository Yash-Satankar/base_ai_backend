"""
Tests for the L1-L8 abstraction-pipeline Pydantic schemas.

Focus: RelationshipItem cardinality normalisation. Different LLMs (llama vs
gpt-oss vs qwen) spell relationship cardinality differently — `N:1`, `M:N`,
`one-to-many` — and the pipeline used to hard-fail the whole blueprint job on
anything outside the canonical {1:1, 1:N, N:M}. It now folds the common
synonyms in, flipping source/target when an `N:1` is rewritten as `1:N`.
"""

import pytest

from app.schemas.abstraction_schemas import RelationshipItem, L5_RelationshipSpec


def _item(rt, src="order_line", tgt="order"):
    return RelationshipItem(
        source_entity=src, target_entity=tgt,
        relationship_type=rt, description="d",
    )


@pytest.mark.parametrize("given,expected", [
    ("1:1", "1:1"),
    ("1:N", "1:N"),
    ("N:M", "N:M"),
    ("1:n", "1:N"),
    ("1:M", "1:N"),
    ("m:n", "N:M"),
    ("M:N", "N:M"),
    ("*:*", "N:M"),
    ("many-to-many", "N:M"),
    ("one-to-one", "1:1"),
    ("one-to-many", "1:N"),
    # UML crow's-foot / optional-cardinality forms (gpt-oss emits these)
    ("1:0..1", "1:1"),
    ("0..1:1", "1:1"),
    ("1:1..1", "1:1"),
    ("1:0..*", "1:N"),
    ("1:0..N", "1:N"),
    ("1:1..*", "1:N"),
    ("1:0..n", "1:N"),
])
def test_forward_synonyms_normalise_without_swapping(given, expected):
    it = _item(given, src="a", tgt="b")
    assert it.relationship_type == expected
    assert (it.source_entity, it.target_entity) == ("a", "b")


@pytest.mark.parametrize("given", [
    "N:1", "n:1", "M:1", "many-to-one",
    "0..n:1", "0..*:1", "*:1", "1..*:1",
])
def test_reverse_cardinality_flips_to_1N_and_swaps_entities(given):
    # "order_line N:1 order" == "order 1:N order_line" (parent first)
    it = _item(given, src="order_line", tgt="order")
    assert it.relationship_type == "1:N"
    assert it.source_entity == "order"
    assert it.target_entity == "order_line"


def test_unknown_value_still_rejected():
    with pytest.raises(Exception):
        _item("belongs-to")


def test_spec_accepts_a_mixed_bag_of_dialects():
    spec = L5_RelationshipSpec(relationships=[
        {"source_entity": "invoice", "target_entity": "line", "relationship_type": "1:N", "description": "d"},
        {"source_entity": "line", "target_entity": "invoice", "relationship_type": "N:1", "description": "d"},
        {"source_entity": "tag", "target_entity": "post", "relationship_type": "M:N", "description": "d"},
    ])
    assert [r.relationship_type for r in spec.relationships] == ["1:N", "1:N", "N:M"]
    # the N:1 row was flipped
    assert (spec.relationships[1].source_entity, spec.relationships[1].target_entity) == ("invoice", "line")


# ── lenient coercion of LLM output-shape quirks (gpt-oss) ───────────

def test_state_transition_coerces_list_valued_states():
    from app.schemas.abstraction_schemas import StateTransition
    st = StateTransition(
        from_state=["Draft", "Submitted", "PendingApproval"],
        to_state="Approved",
        trigger_event=["approve", "sign_off"],
        conditions=[],
    )
    assert st.from_state == "Draft"          # first element wins
    assert st.to_state == "Approved"
    assert st.trigger_event == "approve"


def test_lifecycle_item_coerces_list_entity_name():
    from app.schemas.abstraction_schemas import LifecycleItem
    li = LifecycleItem(entity_name=["journal_voucher", "jv"], states=["a", "b"], transitions=[])
    assert li.entity_name == "journal_voucher"


def test_relationship_ends_coerce_lists():
    it = _item("1:N", src="a", tgt="b")
    assert (it.source_entity, it.target_entity) == ("a", "b")
    from app.schemas.abstraction_schemas import RelationshipItem
    it2 = RelationshipItem(source_entity=["order", "o"], target_entity=["line"],
                           relationship_type="1:0..n", description="d")
    assert it2.source_entity == "order" and it2.target_entity == "line"
    assert it2.relationship_type == "1:N"


@pytest.mark.parametrize("given,expected", [
    ("master", "master"), ("event", "event"), ("lookup", "lookup"),
    ("transaction", "event"), ("txn", "event"), ("audit", "event"), ("fact", "event"),
    ("reference", "lookup"), ("enum", "lookup"), ("category", "lookup"),
    ("link", "junction"), ("associative", "junction"), ("bridge", "junction"),
    ("config", "configuration"), ("setting", "configuration"),
    ("line", "detail"), ("line_item", "detail"), ("line-item", "detail"),
    ("dimension", "master"), ("aggregate", "master"), ("something_odd", "master"),
])
def test_entity_type_normalises(given, expected):
    from app.schemas.abstraction_schemas import EntityItem
    e = EntityItem(name="X", description="d", entity_type=given)
    assert e.entity_type == expected


@pytest.mark.parametrize("given,expected", [
    ("small", "small"), ("MEDIUM", "medium"), ("Large", "large"), ("enterprise", "enterprise"),
    ("xl", "enterprise"), ("huge", "enterprise"), ("startup", "small"), ("big", "large"),
    ("weird", "medium"),
])
def test_l1_scale_normalises(given, expected):
    from app.schemas.abstraction_schemas import L1_UnderstandingSpec
    l1 = L1_UnderstandingSpec(project_name="X", business_goal="g", domain="fintech",
                              target_users=["A"], scale=given)
    assert l1.scale == expected
