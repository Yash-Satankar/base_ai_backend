"""
Tests for the auto-iteration refinement loop (``app/services/schema_refiner.py``)
— now **targeted, per-table** fixes with a whole-schema fallback.

* convergence + non-convergence + the showcase-scale test use a **real MySQL 8**
  (the refiner runs the execution validator every iteration) — skipped when no
  backend, unless ``REQUIRE_MYSQL_EXEC_TESTS=1``.
* cost-attribution + Decision-B degrade + the fallback shrink-guard are
  structural-only (no MySQL) and always run.
"""

import re

import pytest

from app.core.config import settings
from app.conversation import llm_client
from app.services import schema_refiner as sr
from app.services.schema_refiner import (
    refine_until_clean, refine_schemas_until_clean, _table_names, _table_block_map,
)


# ── helpers to build schemas ──────────────────────────────────────

def _clean_table(name: str) -> str:
    return (
        f"CREATE TABLE {name} (\n"
        f"  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        f"  name VARCHAR(120) NOT NULL,\n"
        f"  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',\n"
        f"  created_on DATETIME NOT NULL,\n"
        f"  modified_on DATETIME NOT NULL\n"
        f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )


def _wrap(*tables: str) -> str:
    return (
        "SET FOREIGN_KEY_CHECKS = 0;\n\n"
        + "\n\n".join(tables)
        + "\n\nCOMMIT;\nSET FOREIGN_KEY_CHECKS = 1;\n"
    )


_ID_REGISTRY = (
    "CREATE TABLE unique_id_header_all (\n"
    "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
    "  table_name VARCHAR(100) NOT NULL,\n"
    "  prefix VARCHAR(20) NOT NULL,\n"
    "  last_id VARCHAR(15) NOT NULL DEFAULT '00000',\n"
    "  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',\n"
    "  created_on DATETIME NOT NULL,\n"
    "  modified_on DATETIME NOT NULL\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
)


@pytest.fixture
def targeted_fixer(monkeypatch):
    """LLM stub for TARGETED mode: parses the table names the prompt asks for
    and returns a clean CREATE TABLE for each (plus any 'missing' table)."""
    calls = []

    def fake_call_llm(*, operation, system_prompt, user_prompt, session_id=None,
                      project_id=None, max_tokens=None, model=None, temperature=None,
                      cache=False, degrade=False):
        calls.append(user_prompt)
        m = re.search(r"for each of:\s*([\w,\s]+?)\.\s*\n", user_prompt)
        names = [n.strip() for n in m.group(1).split(",")] if m else []
        names = list(dict.fromkeys(n for n in names if n))
        out = [_clean_table(n) if n != "unique_id_header_all" else _ID_REGISTRY
               for n in names]
        return {"content": "\n\n".join(out), "provider": "together",
                "model": "openai/gpt-oss-120b", "operation": operation,
                "cost_usd": 0.002, "degraded": degrade, "cached": False,
                "usage": {"input_tokens": 800, "output_tokens": 400}}

    monkeypatch.setattr(sr.llm_client, "call_llm", fake_call_llm)
    return calls


@pytest.fixture
def scripted_llm(monkeypatch):
    """LLM stub returning a scripted sequence of raw responses (last repeats)."""
    calls = []

    def _make(responses):
        seq = list(responses)

        def fake_call_llm(*, operation, system_prompt, user_prompt, session_id=None,
                          project_id=None, max_tokens=None, model=None, temperature=None,
                          cache=False, degrade=False):
            calls.append({"operation": operation, "session_id": session_id,
                          "degrade": degrade, "user_prompt": user_prompt})
            content = seq[len(calls) - 1] if len(calls) <= len(seq) else seq[-1]
            return {"content": content, "provider": "together",
                    "model": settings.DEGRADE_MODEL if degrade else settings.TOGETHER_MODEL,
                    "operation": operation, "cost_usd": 0.0021,
                    "degraded": degrade, "cached": False,
                    "usage": {"input_tokens": 3000, "output_tokens": 1200}}

        monkeypatch.setattr(sr.llm_client, "call_llm", fake_call_llm)
        return calls

    return _make


@pytest.fixture
def structural_only(monkeypatch):
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_ENABLED", False)


# ───────────────────────── convergence (real MySQL) ─────────────────────────

def test_targeted_fixes_converge_and_add_missing_registry(mysql_enabled, targeted_fixer):
    """One broken table (MyISAM/latin1/no-timestamps) + a missing id registry.
    Targeted mode fixes the table and appends the registry — converges.
    Named with a NON_ARCHIVABLE_PATTERNS substring ("config") so the
    archive/lifecycle companion nudge doesn't also fire and dilute this
    test's focus on the engine/charset fix + registry addition."""
    seed = _wrap(
        "CREATE TABLE vendor_config_header_all (\n"
        "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        "  name VARCHAR(120) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1\n"
        ") ENGINE=MyISAM DEFAULT CHARSET=latin1;"
    )
    result = refine_until_clean(
        seed, {"requirement": "vendor registry", "system_prompt": "SYS",
               "session_id": "conv-r1"}, max_iterations=3, min_score=0,
    )

    assert result.converged is True
    assert result.iterations_used <= 2
    assert "unique_id_header_all" in result.final_ddl
    assert _table_names(result.final_ddl) == {"vendor_config_header_all", "unique_id_header_all"}
    p0 = targeted_fixer[0]
    assert "vendor_config_header_all" in p0
    assert "non-innodb" in p0.lower() or "innodb" in p0.lower()
    assert "improve this" not in p0.lower()
    assert all(h.get("mode") == "targeted" for h in result.history if h["phase"] == "refine")


def test_unfixable_fk_mismatch_stops_at_cap_and_reports_honestly(mysql_enabled, scripted_llm):
    def _broken(n):
        return (
            "CREATE TABLE p_all (id INT PRIMARY KEY, created_on DATETIME NOT NULL, "
            "modified_on DATETIME NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n"
            "CREATE TABLE c_all (id INT PRIMARY KEY, p_ref VARCHAR(20) NOT NULL, "
            f"created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL, -- try {n}\n"
            "CONSTRAINT fk_c_p FOREIGN KEY (p_ref) REFERENCES p_all (id)) "
            "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
        )

    calls = scripted_llm([_broken(1), _broken(2), _broken(3), _broken(4)])
    result = refine_until_clean(
        _wrap(_broken(0)),
        {"requirement": "x", "system_prompt": "SYS", "session_id": "conv-r2"},
        max_iterations=3, min_score=0,
    )

    assert result.converged is False
    assert result.iterations_used == 3
    assert len(calls) == 3
    assert any(iss["source"] == "mysql" and "incompatible" in iss["message"].lower()
               for iss in result.remaining_issues)
    assert "did NOT converge" in result.summary()
    # the FK error was attributed to c_all → targeted mode
    assert any(h.get("mode") == "targeted" and "c_all" in (h.get("targets") or [])
               for h in result.history if h["phase"] == "refine")


def test_showcase_scale_targeted_refine_no_shrinkage_no_collateral(mysql_enabled, targeted_fixer):
    """32 tables, three broken across the schema. Targeted mode must converge,
    keep all 32, and leave the 29 untouched tables byte-for-byte identical.
    Filler tables are named with a NON_ARCHIVABLE_PATTERNS substring
    ("config") so the archive/lifecycle companion nudge doesn't also fire
    on all 29 of them and dilute this test's focus on splice integrity."""
    good = [_clean_table(f"m{i}_config_header_all") for i in range(29)]
    broken_engine = (
        "CREATE TABLE bad_engine_config_header_all (id INT AUTO_INCREMENT PRIMARY KEY, "
        "name VARCHAR(120) NOT NULL, status INT NOT NULL DEFAULT 1, "
        "created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL) "
        "ENGINE=MyISAM DEFAULT CHARSET=latin1;"
    )
    broken_ts = (
        "CREATE TABLE no_ts_config_header_all (id INT AUTO_INCREMENT PRIMARY KEY, "
        "name VARCHAR(120) NOT NULL, status INT NOT NULL DEFAULT 1) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )
    broken_ai = (
        "CREATE TABLE no_ai_config_header_all (id INT PRIMARY KEY, "
        "name VARCHAR(120) NOT NULL, status INT NOT NULL DEFAULT 1 COMMENT '1=a,2=i', "
        "created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )
    seed = _wrap(_ID_REGISTRY, *good, broken_engine, broken_ts, broken_ai)
    assert len(_table_names(seed)) == 33

    before_blocks = _table_block_map(seed)
    result = refine_until_clean(
        seed, {"requirement": "enterprise ledger", "system_prompt": "SYS",
               "session_id": "conv-scale"}, max_iterations=4, min_score=0,
    )

    assert result.converged is True, result.remaining_issues
    assert len(_table_names(result.final_ddl)) == 33          # nothing dropped/added
    after_blocks = _table_block_map(result.final_ddl)
    touched = {"bad_engine_config_header_all", "no_ts_config_header_all", "no_ai_config_header_all"}
    # every OTHER table is unchanged, byte-for-byte
    for name, (_a, _b, text) in before_blocks.items():
        if name in touched:
            continue
        assert after_blocks[name][2] == text, f"collateral change to {name}"
    # and the mechanism was targeted, no rejections
    refine_hist = [h for h in result.history if h["phase"] == "refine"]
    assert refine_hist and all(h.get("mode") == "targeted" for h in refine_hist)
    assert not any(h.get("rejected_shrunk") or h.get("rejected_integrity") for h in refine_hist)


def test_fk_missing_index_on_referenced_table_targets_the_parent(mysql_enabled, monkeypatch):
    """MySQL 1822 ('Missing index for constraint ... in the referenced table')
    names the PARENT table — the child's FK column is fine, but the parent
    has no key on the column being referenced. The parent must become an
    EDITABLE target (not just read-only FK context for the child), or the
    refiner can never actually fix the root cause."""
    parent_broken = (
        "CREATE TABLE branch_header_all (\n"
        "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        "  branch_code VARCHAR(20) NOT NULL,\n"
        "  name VARCHAR(120) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1,\n"
        "  created_on DATETIME NOT NULL,\n"
        "  modified_on DATETIME NOT NULL\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )
    child = (
        "CREATE TABLE account_header_all (\n"
        "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        "  branch_code VARCHAR(20) NOT NULL,\n"
        "  name VARCHAR(120) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1,\n"
        "  created_on DATETIME NOT NULL,\n"
        "  modified_on DATETIME NOT NULL,\n"
        "  INDEX idx_account_branch_code (branch_code),\n"
        "  CONSTRAINT fk_account_branch FOREIGN KEY (branch_code) "
        "REFERENCES branch_header_all(branch_code) ON DELETE RESTRICT ON UPDATE CASCADE\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )
    seed = _wrap(_ID_REGISTRY, parent_broken, child)

    parent_fixed = (
        "CREATE TABLE branch_header_all (\n"
        "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        "  branch_code VARCHAR(20) NOT NULL,\n"
        "  name VARCHAR(120) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1,\n"
        "  created_on DATETIME NOT NULL,\n"
        "  modified_on DATETIME NOT NULL,\n"
        "  UNIQUE KEY uq_branch_code (branch_code)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )

    calls = []

    def fake_call_llm(*, operation, system_prompt, user_prompt, session_id=None,
                      project_id=None, max_tokens=None, model=None, temperature=None,
                      cache=False, degrade=False):
        calls.append(user_prompt)
        out = {"branch_header_all": parent_fixed, "account_header_all": child}
        return {"content": "\n\n".join(out.values()), "provider": "together",
                "model": "openai/gpt-oss-120b", "operation": operation,
                "cost_usd": 0.002, "degraded": degrade, "cached": False,
                "usage": {"input_tokens": 800, "output_tokens": 400}}

    monkeypatch.setattr(sr.llm_client, "call_llm", fake_call_llm)

    result = refine_until_clean(
        seed, {"requirement": "branches and accounts", "system_prompt": "SYS",
               "session_id": "conv-fk-parent"},
        max_iterations=3, min_score=0,
    )

    assert result.converged is True, result.remaining_issues
    assert len(calls) == 1
    assert "branch_header_all" in calls[0]           # the parent was requested
    assert "uq_branch_code" in result.final_ddl.lower() or "unique key" in result.final_ddl.lower()
    refine_hist = [h for h in result.history if h["phase"] == "refine"]
    assert any(h.get("mode") == "targeted" and "branch_header_all" in (h.get("targets") or [])
               for h in refine_hist)


def _child_fk_table(i: int, fixed: bool = False) -> str:
    action = " ON DELETE CASCADE ON UPDATE CASCADE" if fixed else ""
    return (
        f"CREATE TABLE item{i}_config_header_all (\n"
        f"  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        f"  tenant_id INT NOT NULL,\n"
        f"  name VARCHAR(120) NOT NULL,\n"
        f"  status INT NOT NULL DEFAULT 1,\n"
        f"  created_on DATETIME NOT NULL,\n"
        f"  modified_on DATETIME NOT NULL,\n"
        f"  INDEX idx_item{i}_tenant (tenant_id),\n"
        f"  CONSTRAINT fk_item{i}_tenant FOREIGN KEY (tenant_id) "
        f"REFERENCES tenant_config_header_all(id){action}\n"
        f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )


def test_deferred_fk_type_mismatch_attributes_to_the_declaring_table(mysql_enabled, monkeypatch):
    """MySQL 3780 ('Referencing column X and referenced column Y ... are
    incompatible') can be DEFERRED under SET FOREIGN_KEY_CHECKS=0 when the
    child forward-references a not-yet-created parent: MySQL reports the
    error against whichever CREATE TABLE happens to be executing when the
    deferred check finally resolves (the PARENT, once it's created), not
    the child that actually declares the mismatched column. The generic
    "(at: ...)" table extraction is wrong for this specific error class —
    attribution must instead find whichever table's own CONSTRAINT clause
    names the failing constraint (bed_header_all here, not user_header_all)."""
    # child created BEFORE the parent it references — this ordering is what
    # actually reproduces MySQL attributing the error to the parent instead
    # of the child (verified against real MySQL, not assumed).
    child_bad = (
        "CREATE TABLE bed_header_all (\n"
        "  id BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
        "  bed_code VARCHAR(20) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1,\n"
        "  created_on DATETIME NOT NULL,\n"
        "  modified_on DATETIME NOT NULL,\n"
        "  added_by BIGINT NOT NULL,\n"
        "  INDEX idx_bed_addedby (added_by),\n"
        "  CONSTRAINT fk_bed_addedby FOREIGN KEY (added_by) REFERENCES user_header_all(id)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )
    parent = (
        "CREATE TABLE user_header_all (\n"
        "  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,\n"
        "  name VARCHAR(120) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1,\n"
        "  created_on DATETIME NOT NULL,\n"
        "  modified_on DATETIME NOT NULL\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )
    seed = _wrap(_ID_REGISTRY, child_bad, parent)

    child_fixed = child_bad.replace("added_by BIGINT NOT NULL", "added_by BIGINT UNSIGNED NOT NULL")
    assert child_fixed != child_bad  # sanity: the replace actually matched

    calls = []

    def fake_call_llm(*, operation, system_prompt, user_prompt, session_id=None,
                      project_id=None, max_tokens=None, model=None, temperature=None,
                      cache=False, degrade=False):
        calls.append(user_prompt)
        return {"content": child_fixed, "provider": "together",
                "model": "openai/gpt-oss-120b", "operation": operation,
                "cost_usd": 0.002, "degraded": degrade, "cached": False,
                "usage": {"input_tokens": 500, "output_tokens": 200}}

    monkeypatch.setattr(sr.llm_client, "call_llm", fake_call_llm)

    result = refine_until_clean(
        seed, {"requirement": "hospital beds", "system_prompt": "SYS",
               "session_id": "conv-deferred-fk"},
        max_iterations=3, min_score=0,
    )

    assert result.converged is True, result.remaining_issues
    assert len(calls) == 1
    assert "bed_header_all" in calls[0]        # correctly targeted the DECLARING table
    refine_hist = [h for h in result.history if h["phase"] == "refine"]
    assert any(h.get("mode") == "targeted" and "bed_header_all" in (h.get("targets") or [])
               for h in refine_hist)
    # the parent was never asked to change — it was never the real problem
    assert not any("user_header_all" in (h.get("targets") or []) for h in refine_hist)


def test_broad_shallow_finding_sweeps_every_implicated_table_across_iterations(mysql_enabled, monkeypatch):
    """A systemic-but-shallow finding (every FK left at the implicit RESTRICT)
    can implicate EVERY table in an enterprise-scale schema at once. 25
    tables each carry one such finding — more than _MAX_TARGET_TABLES=20, so
    one iteration can only cover a capped subset. A larger per-call cap was
    tried and empirically made real showcase-scale fixes LESS reliable (the
    model drops fixes once a single call spans 40+ tables) — so coverage
    beyond the cap must come from spending a second iteration re-attributing
    whatever is still broken, not from raising the cap further."""
    n = 25
    tenant = (
        "CREATE TABLE tenant_config_header_all (\n"
        "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        "  name VARCHAR(120) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1,\n"
        "  created_on DATETIME NOT NULL,\n"
        "  modified_on DATETIME NOT NULL\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )
    seed = _wrap(_ID_REGISTRY, tenant, *[_child_fk_table(i) for i in range(n)])
    assert len(_table_names(seed)) == n + 2

    calls = []

    def fake_call_llm(*, operation, system_prompt, user_prompt, session_id=None,
                      project_id=None, max_tokens=None, model=None, temperature=None,
                      cache=False, degrade=False):
        calls.append(user_prompt)
        m = re.search(r"for each of:\s*([\w,\s]+?)\.\s*\n", user_prompt)
        names = [n2.strip() for n2 in m.group(1).split(",")] if m else []
        out = []
        for name in dict.fromkeys(n2 for n2 in names if n2):
            if name.startswith("item") and name.endswith("_config_header_all"):
                idx = int(name[len("item"):-len("_config_header_all")])
                out.append(_child_fk_table(idx, fixed=True))
        return {"content": "\n\n".join(out), "provider": "together",
                "model": "openai/gpt-oss-120b", "operation": operation,
                "cost_usd": 0.01, "degraded": degrade, "cached": False,
                "usage": {"input_tokens": 4000, "output_tokens": 3000}}

    monkeypatch.setattr(sr.llm_client, "call_llm", fake_call_llm)

    result = refine_until_clean(
        seed, {"requirement": "multi-tenant catalog", "system_prompt": "SYS",
               "session_id": "conv-broad-fk"},
        max_iterations=3, min_score=0, advisory_threshold=0,
    )

    assert result.converged is True, result.remaining_issues
    assert result.iterations_used == 2, "20-table cap needs a 2nd pass to cover all 25"
    assert len(calls) == 2
    refine_hist = [h for h in result.history if h["phase"] == "refine"]
    assert len(refine_hist) == 2
    assert all(h["mode"] == "targeted" for h in refine_hist)
    assert len(refine_hist[0]["targets"]) == 20
    all_targeted = set(refine_hist[0]["targets"]) | set(refine_hist[1]["targets"])
    assert all_targeted == {f"item{i}_config_header_all" for i in range(n)}


def test_duplicate_index_from_standalone_create_index_targets_the_table(mysql_enabled, monkeypatch):
    """MySQL 'Duplicate key name' from a standalone `CREATE INDEX ... ON
    <table> (...)` statement (not a CREATE/ALTER TABLE) doesn't match the
    generic DDL-error table regex — it must still resolve to the table it
    targets so the fix stays targeted, not an unattributable whole-rewrite."""
    table_with_dupe = (
        "CREATE TABLE item_header_all (\n"
        "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        "  item_code VARCHAR(30) NOT NULL,\n"
        "  name VARCHAR(120) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1,\n"
        "  created_on DATETIME NOT NULL,\n"
        "  modified_on DATETIME NOT NULL,\n"
        "  INDEX idx_item_code (item_code)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )
    standalone_index = "CREATE INDEX idx_item_code ON item_header_all (item_code);"
    seed = _wrap(_ID_REGISTRY, table_with_dupe, standalone_index)

    fixed_table = (
        "CREATE TABLE item_header_all (\n"
        "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        "  item_code VARCHAR(30) NOT NULL,\n"
        "  name VARCHAR(120) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1,\n"
        "  created_on DATETIME NOT NULL,\n"
        "  modified_on DATETIME NOT NULL\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )

    calls = []

    def fake_call_llm(*, operation, system_prompt, user_prompt, session_id=None,
                      project_id=None, max_tokens=None, model=None, temperature=None,
                      cache=False, degrade=False):
        calls.append(user_prompt)
        return {"content": fixed_table, "provider": "together",
                "model": "openai/gpt-oss-120b", "operation": operation,
                "cost_usd": 0.002, "degraded": degrade, "cached": False,
                "usage": {"input_tokens": 500, "output_tokens": 200}}

    monkeypatch.setattr(sr.llm_client, "call_llm", fake_call_llm)

    result = refine_until_clean(
        seed, {"requirement": "catalog", "system_prompt": "SYS", "session_id": "conv-dupe-idx"},
        max_iterations=3, min_score=0,
    )

    assert result.converged is True, result.remaining_issues
    assert len(calls) == 1
    assert "item_header_all" in calls[0]
    refine_hist = [h for h in result.history if h["phase"] == "refine"]
    assert any(h.get("mode") == "targeted" and "item_header_all" in (h.get("targets") or [])
               for h in refine_hist)


def test_fk_business_column_naming_is_attributed_to_the_child_table(mysql_enabled, monkeypatch):
    """rule-31 ('FK references business column ... instead of id PK') used
    to run as one global regex with no table context, so every match had
    table_name=None and fell into schemawide — silently starved whenever ANY
    other issue in the same schema was independently attributable (targeted
    mode wins and schemawide findings never even reach the prompt that
    iteration). It's now checked per table; confirm it resolves to the
    table that HAS the offending FK and that enough of these (one per child,
    cumulatively enough to drop the default min_score=90 gate) actually get
    fixed and converge. Table names carry a NON_ARCHIVABLE_PATTERNS
    substring ("config") so the archive/lifecycle companion nudge doesn't
    also fire and entangle this test with an unrelated mechanism."""
    parent = (
        "CREATE TABLE patient_config_header_all (\n"
        "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        "  patient_no VARCHAR(20) NOT NULL UNIQUE,\n"
        "  name VARCHAR(120) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',\n"
        "  created_on DATETIME NOT NULL,\n"
        "  modified_on DATETIME NOT NULL\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )
    entities = ["encounter", "lab_order", "prescription", "billing", "consent", "vital_sign"]

    def bad_child(name: str) -> str:
        return (
            f"CREATE TABLE {name}_config_header_all (\n"
            f"  id INT AUTO_INCREMENT PRIMARY KEY,\n"
            f"  patient_no VARCHAR(20) NOT NULL,\n"
            f"  name VARCHAR(120) NOT NULL,\n"
            f"  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',\n"
            f"  created_on DATETIME NOT NULL,\n"
            f"  modified_on DATETIME NOT NULL,\n"
            f"  INDEX idx_{name}_patient (patient_no),\n"
            f"  CONSTRAINT fk_{name}_patient FOREIGN KEY (patient_no) "
            f"REFERENCES patient_config_header_all(patient_no) "
            f"ON DELETE CASCADE ON UPDATE CASCADE\n"
            f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
        )

    def fixed_child(name: str) -> str:
        return (
            f"CREATE TABLE {name}_config_header_all (\n"
            f"  id INT AUTO_INCREMENT PRIMARY KEY,\n"
            f"  patient_id INT NOT NULL,\n"
            f"  name VARCHAR(120) NOT NULL,\n"
            f"  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',\n"
            f"  created_on DATETIME NOT NULL,\n"
            f"  modified_on DATETIME NOT NULL,\n"
            f"  INDEX idx_{name}_patient (patient_id),\n"
            f"  CONSTRAINT fk_{name}_patient FOREIGN KEY (patient_id) "
            f"REFERENCES patient_config_header_all(id) "
            f"ON DELETE CASCADE ON UPDATE CASCADE\n"
            f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
        )

    seed = _wrap(_ID_REGISTRY, parent, *[bad_child(e) for e in entities])

    from app.validators.schema_validator import SchemaValidator
    struct0 = SchemaValidator().validate(seed)
    assert not [i for i in struct0.issues if i.severity in ("critical", "high")]
    # index_constraints (weight 10) floors at -10 once 5+ medium findings hit
    # it, so score bottoms out at exactly 90 regardless of how many more
    # business-column FKs pile on — use a min_score of 91 to force at least
    # one iteration rather than fighting a floor that a real showcase run
    # wouldn't hit in isolation either (it always coexists with other dims).
    assert struct0.score == 90
    rule31 = [i for i in struct0.issues if i.rule_id == 31]
    assert len(rule31) == len(entities)
    assert all(i.table_name and i.table_name.endswith("_config_header_all") for i in rule31)

    calls = []

    def fake_call_llm(*, operation, system_prompt, user_prompt, session_id=None,
                      project_id=None, max_tokens=None, model=None, temperature=None,
                      cache=False, degrade=False):
        calls.append(user_prompt)
        m = re.search(r"for each of:\s*([\w,\s]+?)\.\s*\n", user_prompt)
        names = [n.strip() for n in m.group(1).split(",")] if m else []
        out = []
        for n in dict.fromkeys(x for x in names if x):
            if n != "patient_config_header_all" and n.endswith("_config_header_all"):
                out.append(fixed_child(n[: -len("_config_header_all")]))
        return {"content": "\n\n".join(out), "provider": "together",
                "model": "openai/gpt-oss-120b", "operation": operation,
                "cost_usd": 0.01, "degraded": degrade, "cached": False,
                "usage": {"input_tokens": 3000, "output_tokens": 2000}}

    monkeypatch.setattr(sr.llm_client, "call_llm", fake_call_llm)

    result = refine_until_clean(
        seed, {"requirement": "hospital encounters", "system_prompt": "SYS",
               "session_id": "conv-fk-naming"},
        max_iterations=3, min_score=91,
    )

    assert result.converged is True, result.remaining_issues
    assert len(calls) == 1, "cap=60 should sweep all 6 implicated tables in one targeted pass"
    refine_hist = [h for h in result.history if h["phase"] == "refine"]
    assert refine_hist[0]["mode"] == "targeted"
    assert set(refine_hist[0]["targets"]) == {f"{e}_config_header_all" for e in entities}


# ─────────────────── cost attribution (structural only) ───────────────────

_BAD_NAME_SEED = _wrap(
    "CREATE TABLE widgets (id INT AUTO_INCREMENT PRIMARY KEY, "
    "name VARCHAR(100) NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
)


@pytest.fixture
def priced_llm(monkeypatch):
    """Real llm_client.call_llm, with only the inner model call + telemetry
    stubbed — so cost accounting / operation tagging / degrade routing run."""
    ops = []
    n = {"i": 0}

    def fake_generate_schema(system_prompt, user_prompt, max_tokens=None, model=None, temperature=None):
        n["i"] += 1
        # return the 'widgets' table again, still unfixed, but a byte different
        return {"content": f"CREATE TABLE widgets (id INT AUTO_INCREMENT PRIMARY KEY, "
                           f"name VARCHAR({100 + n['i']}) NOT NULL) "
                           f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;",
                "provider": "together", "model": model or settings.TOGETHER_MODEL,
                "usage": {"input_tokens": 500, "output_tokens": 200}}

    real_log = llm_client.TelemetryManager.log_operation

    def spy_log(**kw):
        ops.append(kw.get("operation"))
        return real_log(**kw)

    monkeypatch.setattr(llm_client, "generate_schema", fake_generate_schema)
    monkeypatch.setattr(llm_client.TelemetryManager, "log_operation", spy_log)
    return ops


def test_cost_attributed_to_conversation_per_iteration(structural_only, priced_llm):
    sid = "conv-cost"
    assert llm_client.conversation_cost(sid) == 0.0

    result = refine_until_clean(
        _BAD_NAME_SEED,
        {"requirement": "widgets", "system_prompt": "SYS", "session_id": sid},
        max_iterations=3, min_score=0,
    )

    assert result.iterations_used == 3
    assert priced_llm == ["schema_refine", "schema_refine", "schema_refine"]
    refine_records = [h for h in result.history if h["phase"] == "refine"]
    assert len(refine_records) == 3
    assert all(r["cost_usd"] > 0 for r in refine_records)
    assert result.total_cost_usd == pytest.approx(sum(r["cost_usd"] for r in refine_records))
    assert llm_client.conversation_cost(sid) == pytest.approx(result.total_cost_usd, rel=1e-3)


# ─────────────────── Decision-B degrade (structural only) ───────────────────

def test_degraded_conversation_is_capped_to_one_iteration(structural_only, priced_llm, monkeypatch):
    monkeypatch.setattr(settings, "CONVERSATION_COST_WARN_USD", 0.001)
    sid = "conv-degraded"
    llm_client._add_conversation_cost(sid, 0.01)
    assert llm_client.should_degrade(sid) is True

    result = refine_until_clean(
        _BAD_NAME_SEED,
        {"requirement": "widgets", "system_prompt": "SYS", "session_id": sid},
        max_iterations=3, min_score=0,
    )

    assert result.degraded is True
    assert result.iterations_used == 1
    assert priced_llm == ["schema_refine"]
    assert result.converged is False
    assert result.remaining_issues


def test_explicit_degraded_flag_overrides_auto_detection(structural_only, targeted_fixer):
    result = refine_until_clean(
        _BAD_NAME_SEED,
        {"requirement": "widgets", "system_prompt": "SYS", "session_id": "conv-x"},
        max_iterations=3, min_score=0, degraded=True,
    )
    assert result.degraded is True
    assert result.iterations_used == 1
    assert len(targeted_fixer) == 1


# ─────────────────── whole-schema fallback + shrink guard ───────────────────

def test_whole_schema_fallback_when_issue_is_not_table_attributable(structural_only, scripted_llm):
    """A GST/financial rule with no attributable table and no transaction table
    to target → whole-schema mode. A rewrite that drops tables is discarded."""
    # 8 "config" tables (NON_ARCHIVABLE — no archive/lifecycle nudge, so they
    # carry no OTHER attributable issue) + one with a money column but no
    # transaction table anywhere → rule-7 (GST) fires schema-wide, unattributable.
    money_tbl = (
        "CREATE TABLE price_book_config_header_all (id INT AUTO_INCREMENT PRIMARY KEY, "
        "list_price DECIMAL(12,2) NOT NULL, status INT NOT NULL DEFAULT 1, "
        "created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )
    seed = _wrap(_ID_REGISTRY, *[_clean_table(f"c{i}_config_header_all") for i in range(8)], money_tbl)
    n_tables = len(_table_names(seed))

    shrunk = _wrap(_clean_table("c0_config_header_all"), _clean_table("c1_config_header_all"))
    calls = scripted_llm([shrunk, shrunk, shrunk])

    result = refine_until_clean(
        seed, {"requirement": "pricing", "system_prompt": "SYS", "session_id": "conv-whole"},
        max_iterations=3, min_score=0,
    )

    refine_hist = [h for h in result.history if h["phase"] == "refine"]
    assert refine_hist and refine_hist[0].get("mode") == "whole"
    assert any(h.get("rejected_shrunk") for h in refine_hist)
    assert len(_table_names(result.final_ddl)) == n_tables      # shrunk rewrite discarded
    assert len(calls) == 1


def test_splice_integrity_catches_drops_dups_and_collateral_edits():
    """Unit test the splice-integrity guard directly — it must reject a spliced
    result that dropped, duplicated, or silently mutated a table outside the
    ones intentionally touched."""
    before = _wrap(_clean_table("a_all"), _clean_table("b_all"), _clean_table("c_all"))

    # 1. clean targeted edit of b_all → OK
    ok_after = before.replace(
        "  name VARCHAR(120) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',\n"
        "  created_on DATETIME NOT NULL,\n"
        "  modified_on DATETIME NOT NULL\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;",
        "  name VARCHAR(200) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',\n"
        "  created_on DATETIME NOT NULL,\n"
        "  modified_on DATETIME NOT NULL\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;",
        1,   # only the first occurrence (a_all) — pretend a_all was the target
    )
    ok, problems = sr._splice_integrity(before, ok_after, touched={"a_all"}, added=set())
    assert ok, problems

    # 2. dropped a table
    dropped = _wrap(_clean_table("a_all"), _clean_table("c_all"))
    ok, problems = sr._splice_integrity(before, dropped, touched={"a_all"}, added=set())
    assert not ok and any("dropped" in p for p in problems)

    # 3. duplicated a table
    dup = _wrap(_clean_table("a_all"), _clean_table("b_all"), _clean_table("b_all"),
                _clean_table("c_all"))
    ok, problems = sr._splice_integrity(before, dup, touched={"b_all"}, added=set())
    assert not ok and any("duplicate" in p for p in problems)

    # 4. collateral change to a table that was NOT the target
    collateral = ok_after   # a_all changed, but we claim we only touched b_all
    ok, problems = sr._splice_integrity(before, collateral, touched={"b_all"}, added=set())
    assert not ok and any("untouched table 'a_all' changed" in p for p in problems)


# ─────────────────── score threshold + companion tables ───────────────────

def test_low_severity_findings_block_convergence_until_score_clears_min(mysql_enabled, targeted_fixer):
    """A schema with NO blocking issues, 0 advisories, but several header
    tables missing their archive/lifecycle companions (low severity,
    score-only — cumulative across tables) must NOT converge at the default
    SCHEMA_REFINE_MIN_SCORE=90 — and the refiner must ask for the missing
    companion tables by name. This nudge is scoped to high-criticality
    domains (docs/enterprise_standards_spec.md §2.1/§2.5), so both the
    sanity-check validate() call and the refine context opt in explicitly —
    this test represents a financial/regulated caller, not the default."""
    entities = ["vendor", "customer", "product", "order", "shipment", "employee", "warehouse"]
    seed = _wrap(_ID_REGISTRY, *[_clean_table(f"{e}_header_all") for e in entities])

    # sanity: this schema has no blocking issues and is MySQL-clean already
    from app.validators.schema_validator import SchemaValidator
    struct0 = SchemaValidator().validate(seed, high_criticality=True)
    assert not [i for i in struct0.issues if i.severity in ("critical", "high")]
    assert struct0.score < 90   # 7 tables x 2 missing companions each costs enough to matter

    result = refine_until_clean(
        seed, {"requirement": "vendor registry", "system_prompt": "SYS",
               "session_id": "conv-minscore", "high_criticality": True},
        max_iterations=3,   # default min_score (90) applies
    )

    assert "vendor_archive_all" in result.final_ddl
    # it was requested as a missing companion table, not a random whole-rewrite
    assert any("vendor_archive_all" in p for p in targeted_fixer)


def test_min_score_zero_accepts_the_same_schema_as_converged(structural_only, priced_llm):
    """Same shape of gap (no blocking, no advisories, same high-criticality
    scoping) but with min_score=0 the schema converges immediately —
    proving the low-severity gate is what changed the outcome above, not
    the high_criticality flag or some other refiner behaviour."""
    entities = ["vendor", "customer", "product", "order", "shipment", "employee", "warehouse"]
    seed = _wrap(_ID_REGISTRY, *[_clean_table(f"{e}_header_all") for e in entities])
    result = refine_until_clean(
        seed, {"requirement": "vendor registry", "system_prompt": "SYS",
               "session_id": "conv-minscore0", "high_criticality": True},
        max_iterations=3, min_score=0,
    )
    assert result.converged is True
    assert result.iterations_used == 0


def test_preservation_nudge_exempt_by_default_even_with_missing_companions(mysql_enabled):
    """Same schema as above (several header tables missing archive/lifecycle
    companions) but WITHOUT high_criticality — the default. The nudge must
    not fire at all, and the schema must score high enough to converge on
    its own, with zero LLM calls needed for this class of finding."""
    entities = ["vendor", "customer", "product", "order", "shipment", "employee", "warehouse"]
    seed = _wrap(_ID_REGISTRY, *[_clean_table(f"{e}_header_all") for e in entities])

    from app.validators.schema_validator import SchemaValidator
    struct0 = SchemaValidator().validate(seed)  # high_criticality defaults to False
    assert not [i for i in struct0.issues if i.rule_id in (3, 4)]
    assert struct0.score == 100


# ─────────────────── schema decomposition (per-schema refinement) ─────────
# docs/enterprise_standards_spec.md §2.2/§2.4. refine_until_clean itself is
# untouched by these — refine_schemas_until_clean is a thin per-schema
# orchestration wrapper around it, so the single-schema path above is
# provably unaffected by this capability existing.

def test_decomposed_schemas_refine_independently_touching_only_the_broken_one(mysql_enabled, targeted_fixer):
    """Two schema-modules: 'billing' is already clean, 'clinical' has one
    broken table (MyISAM engine). Each must be validated and fixed
    independently — the clean schema's DDL must come back byte-identical
    (never touched, never even sent to the LLM), and the fix for the broken
    schema must never see the other schema's tables."""
    billing_ddl = _wrap(_ID_REGISTRY, _clean_table("subscription_header_all"))
    clinical_broken = (
        "CREATE TABLE patient_header_all (\n"
        "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        "  name VARCHAR(120) NOT NULL,\n"
        "  status INT NOT NULL DEFAULT 1,\n"
        "  created_on DATETIME NOT NULL,\n"
        "  modified_on DATETIME NOT NULL\n"
        ") ENGINE=MyISAM DEFAULT CHARSET=latin1;"
    )
    clinical_ddl = _wrap(_ID_REGISTRY, clinical_broken)

    results = refine_schemas_until_clean(
        {"billing": billing_ddl, "clinical": clinical_ddl},
        {"requirement": "multi-schema project", "system_prompt": "SYS",
         "session_id": "conv-decomp"},
        max_iterations=3, min_score=0,
    )

    assert set(results) == {"billing", "clinical"}

    assert results["billing"].converged is True
    assert results["billing"].iterations_used == 0
    assert results["billing"].final_ddl == billing_ddl        # never touched

    assert results["clinical"].converged is True
    assert results["clinical"].iterations_used >= 1
    assert "patient_header_all" in targeted_fixer[0]
    # scoped to the clinical schema's own DDL — billing's table never leaks in
    assert "subscription_header_all" not in targeted_fixer[0]


def test_single_schema_dict_matches_direct_refine_until_clean(mysql_enabled):
    """refine_schemas_until_clean with exactly one schema must behave
    identically to calling refine_until_clean directly on that same DDL —
    the wrapper introduces no behavior change for a single schema."""
    ddl = _wrap(_ID_REGISTRY, _clean_table("vendor_header_all"))
    direct = refine_until_clean(
        ddl, {"requirement": "r", "system_prompt": "SYS", "session_id": "conv-direct"},
        max_iterations=3, min_score=0,
    )
    wrapped = refine_schemas_until_clean(
        {"only": ddl}, {"requirement": "r", "system_prompt": "SYS", "session_id": "conv-direct"},
        max_iterations=3, min_score=0,
    )["only"]

    assert wrapped.converged == direct.converged
    assert wrapped.final_ddl == direct.final_ddl
    assert wrapped.iterations_used == direct.iterations_used
