"""
Integrity checks for app/rules/rules.json — the rule library that drives
RAG retrieval, prompt injection and schema validation.

These tests lock down structural invariants so a hand-edit to the JSON that
breaks a cross-reference (rule_service, schema_validator) fails loudly here
instead of silently degrading generation quality.
"""

import json
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
RULES_PATH = APP / "rules" / "rules.json"

REQUIRED_KEYS = (
    "rule_id",
    "category",
    "rule_name",
    "priority",
    "trigger_when",
    "enforce",
    "reason",
    "tags",
)


@pytest.fixture(scope="module")
def data() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rules(data) -> list[dict]:
    return data["rules"]


@pytest.fixture(scope="module")
def rule_ids(rules) -> set[int]:
    return {r["rule_id"] for r in rules}


# ── Structural invariants ──────────────────────────────────────────

def test_json_parses_and_has_rules(data):
    assert isinstance(data.get("rules"), list)
    assert len(data["rules"]) > 0


def test_every_rule_has_required_keys(rules):
    problems = []
    for r in rules:
        for k in REQUIRED_KEYS:
            if k not in r or r[k] in ("", [], {}):
                problems.append((r.get("rule_id"), k))
    assert not problems, f"rules missing required keys: {problems}"


def test_rule_ids_are_unique_positive_ints(rules):
    ids = [r["rule_id"] for r in rules]
    assert all(isinstance(i, int) and i > 0 for i in ids)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate rule_id(s): {dupes}"


def test_total_rules_matches_actual_count(data, rules):
    assert data["metadata"]["total_rules"] == len(rules)


def test_priorities_are_declared(data, rules):
    declared = set(data["metadata"]["priority_levels"])
    used = {r["priority"] for r in rules}
    assert used <= declared, f"undeclared priorities in use: {used - declared}"


def test_every_used_category_is_declared_in_metadata(data, rules):
    declared = set(data["metadata"]["categories"])
    used = {r["category"] for r in rules}
    missing = sorted(used - declared)
    assert not missing, f"categories used by rules but absent from metadata.categories: {missing}"


def test_metadata_category_list_has_no_dead_entries(data, rules):
    declared = set(data["metadata"]["categories"])
    used = {r["category"] for r in rules}
    dead = sorted(declared - used)
    assert not dead, f"metadata.categories lists categories no rule uses: {dead}"


def test_trigger_when_and_enforce_are_nonempty_string_lists(rules):
    for r in rules:
        for field in ("trigger_when", "enforce", "tags"):
            val = r[field]
            assert isinstance(val, list) and val, f"rule {r['rule_id']}: {field} must be a non-empty list"
            assert all(isinstance(x, str) and x.strip() for x in val), (
                f"rule {r['rule_id']}: {field} has empty/non-string entries"
            )


# ── Cross-reference invariants ────────────────────────────────────

def test_rule_service_mandatory_rules_all_exist(rule_ids):
    from app.services.rule_service import DOMAIN_MANDATORY_RULES, UNIVERSAL_RULES

    bad = {}
    for domain, ids in DOMAIN_MANDATORY_RULES.items():
        missing = sorted(set(ids) - rule_ids)
        if missing:
            bad[domain] = missing
    missing_universal = sorted(set(UNIVERSAL_RULES) - rule_ids)
    assert not bad, f"DOMAIN_MANDATORY_RULES reference non-existent rule_ids: {bad}"
    assert not missing_universal, f"UNIVERSAL_RULES reference non-existent rule_ids: {missing_universal}"


def test_production_hardening_rules_present(rules):
    """Rules added to close the gaps between rules.json and a real
    production database. Guard against accidental removal."""
    by_id = {r["rule_id"]: r for r in rules}

    assert 21 in by_id, "missing rule 21 (InnoDB storage engine)"
    assert "innodb" in by_id[21]["tags"]
    assert by_id[21]["priority"] == "critical"

    assert 22 in by_id, "missing rule 22 (utf8mb4 charset)"
    assert "utf8mb4" in by_id[22]["tags"]

    assert 23 in by_id, "missing rule 23 (temporal column data types)"
    assert "datetime" in by_id[23]["tags"]

    assert 28 in by_id, "missing rule 28 (old_ value preservation mechanics)"
    assert "layer_2" in by_id[28]["tags"]


def test_schema_validator_dimension_map_all_exist(rule_ids):
    sv_src = (APP / "validators" / "schema_validator.py").read_text(encoding="utf-8")
    block = re.search(r"RULE_TO_DIMENSION\s*=\s*\{(.+?)\n\s*\}", sv_src, re.S).group(1)
    referenced = set(map(int, re.findall(r"\b(\d+)\b", block)))
    missing = sorted(referenced - rule_ids)
    assert not missing, f"schema_validator RULE_TO_DIMENSION references non-existent rule_ids: {missing}"
