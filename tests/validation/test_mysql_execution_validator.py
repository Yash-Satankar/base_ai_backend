"""
Tests for the MySQL *execution* validation gate
(``app/services/mysql_execution_validator.py``).

Two groups:

* **No-backend unit tests** — always run. They cover the flag gate, the
  graceful-skip path, and the combined-grade fold. No MySQL needed.

* **Real-MySQL tests** — need a live MySQL 8. They use, in order:
    1. ``MYSQL_EXEC_VALIDATION_DSN`` if set in the environment, else
    2. an ephemeral ``testcontainers`` MySQL 8 (needs a running Docker).
  If neither is available the whole group is **skipped**, not failed —
  e.g. ``docker compose --profile validation up -d mysql-test`` then
  ``MYSQL_EXEC_VALIDATION_DSN=mysql://root:test@localhost:3310/ pytest``.

Every real-MySQL case has a positive and a negative check. The checks
themselves are grounded in documented best practice, not observed compliance
in ``Databases/`` — see ``docs/enterprise_standards_spec.md``.
"""

import pytest

from app.services.mysql_execution_validator import (
    ExecutionResult,
    execute_and_validate,
    execute_and_validate_schemas,
    _NoBackend,
    _check_partition_fk_conflict,
    _check_cross_schema_fk_violations,
)
from app.services import mysql_execution_validator as mev
from app.services.planner_helpers import run_execution_gate, fold_execution_into_grade


# ─────────────────────────────────────────────────────────────────────
# No-backend unit tests (always run)
# ─────────────────────────────────────────────────────────────────────

def test_gate_returns_none_when_flag_disabled(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_ENABLED", False)
    assert run_execution_gate("CREATE TABLE x_all (id INT);") is None


def test_execute_and_validate_skips_gracefully_without_backend(monkeypatch):
    """No Docker, no DSN → skipped=True, success=False, and NO exception."""
    def _boom():
        raise _NoBackend("no backend for test")

    monkeypatch.setattr(mev, "_acquire_mysql", _boom)
    r = execute_and_validate("CREATE TABLE x_all (id INT PRIMARY KEY);")
    assert isinstance(r, ExecutionResult)
    assert r.skipped is True
    assert r.success is False
    assert r.executed is False
    assert "no backend for test" in r.skip_reason
    assert "skipped" in r.summary().lower()


def test_gate_swallows_module_errors(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_ENABLED", True)

    def _raise(_sql):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mev, "execute_and_validate", _raise)
    out = run_execution_gate("CREATE TABLE x_all (id INT);")
    assert out["skipped"] is True
    assert "kaboom" in out["skip_reason"]


def test_fold_grade_no_execution():
    v = fold_execution_into_grade(85, None)
    assert v == {
        "combined_score": 85, "combined_grade": "B", "combined_passed": True,
        "execution_ran": False, "notes": [],
    }


def test_fold_grade_skipped_execution_is_neutral():
    v = fold_execution_into_grade(85, {"skipped": True, "skip_reason": "no docker"})
    assert v["combined_passed"] is True
    assert v["execution_ran"] is False
    assert "skipped" in v["notes"][0]


def test_fold_grade_ddl_failure_fails_regardless_of_structural_score():
    v = fold_execution_into_grade(
        95,
        {"skipped": False, "success": False, "ddl_errors": ["[MySQL 1064] boom"],
         "error_issue_count": 0, "advisory_issue_count": 0},
    )
    assert v["combined_passed"] is False
    assert v["combined_score"] <= 55
    assert v["combined_grade"] == "F"


def test_fold_grade_enterprise_errors_fail_advisories_only_nudge():
    fail = fold_execution_into_grade(
        90, {"skipped": False, "success": True, "ddl_errors": [],
             "error_issue_count": 1, "advisory_issue_count": 0})
    assert fail["combined_passed"] is False

    nudge = fold_execution_into_grade(
        90, {"skipped": False, "success": True, "ddl_errors": [],
             "error_issue_count": 0, "advisory_issue_count": 3})
    assert nudge["combined_passed"] is True
    assert nudge["combined_score"] == 84  # 90 - 2*3


# ── partition/FK conflict — pure function, no MySQL needed (§1c) ──────
# Not wired into _enterprise_checks yet (nothing in the generator emits
# PARTITION BY today) — these test the function directly so it's already
# proven correct whenever a call site is added.

def test_partition_fk_conflict_flags_partitioned_child():
    fks = [{"name": "fk_x", "child_table": "big_transaction_all", "child_col": "a",
            "parent_table": "ref_all", "parent_col": "id",
            "delete_rule": "RESTRICT", "update_rule": "RESTRICT"}]
    issues = _check_partition_fk_conflict({"big_transaction_all"}, fks)
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].category == "partition-fk-conflict"
    assert issues[0].table == "big_transaction_all"


def test_partition_fk_conflict_flags_partitioned_parent():
    fks = [{"name": "fk_x", "child_table": "child_all", "child_col": "a",
            "parent_table": "big_all", "parent_col": "id",
            "delete_rule": "RESTRICT", "update_rule": "RESTRICT"}]
    issues = _check_partition_fk_conflict({"big_all"}, fks)
    assert len(issues) == 1
    assert issues[0].table == "big_all"


def test_partition_fk_conflict_clean_when_nothing_partitioned():
    fks = [{"name": "fk_x", "child_table": "child_all", "child_col": "a",
            "parent_table": "parent_all", "parent_col": "id",
            "delete_rule": "RESTRICT", "update_rule": "RESTRICT"}]
    assert _check_partition_fk_conflict(set(), fks) == []


# ─────────────────────────────────────────────────────────────────────
# Real-MySQL tests
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def run_ddl(_mysql_dsn, monkeypatch):
    """Return ``execute_and_validate`` wired to the test MySQL."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_ENABLED", True)
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_DSN", _mysql_dsn)
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_USE_TESTCONTAINER", False)
    monkeypatch.setenv("MYSQL_EXEC_VALIDATION_DSN", _mysql_dsn)
    return execute_and_validate


# ── a clean 3-layer schema in the house style ──────────────────────
VALID_SCHEMA = """
SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE company_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  company_id VARCHAR(20) NOT NULL,
  name VARCHAR(150) NOT NULL,
  status TINYINT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL,
  UNIQUE KEY uq_company_company_id (company_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE branch_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  branch_id VARCHAR(20) NOT NULL,
  company_id INT NOT NULL,
  name VARCHAR(150) NOT NULL,
  status TINYINT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL,
  KEY idx_branch_company (company_id),
  CONSTRAINT fk_branch_company FOREIGN KEY (company_id)
    REFERENCES company_header_all (id) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET FOREIGN_KEY_CHECKS = 1;
"""


def test_valid_schema_executes_clean(run_ddl):
    r = run_ddl(VALID_SCHEMA)
    assert r.executed is True
    assert r.skipped is False
    assert r.ddl_errors == [], r.ddl_errors
    assert r.success is True
    assert set(r.tables_created) == {"company_header_all", "branch_header_all"}
    assert not r.error_issues
    assert r.enterprise_score >= 95


# ── engine errors are surfaced verbatim, not paraphrased ───────────

def test_fk_type_mismatch_reports_real_mysql_error(run_ddl):
    """child FK column VARCHAR(20) → parent id INT. MySQL 8 rejects this at
    CREATE with errno 3780; the exact engine wording must come through."""
    ddl = """
    CREATE TABLE parent_all (id INT AUTO_INCREMENT PRIMARY KEY) ENGINE=InnoDB;
    CREATE TABLE child_all (
      id INT AUTO_INCREMENT PRIMARY KEY,
      parent_ref VARCHAR(20) NOT NULL,
      CONSTRAINT fk_child_parent FOREIGN KEY (parent_ref) REFERENCES parent_all (id)
    ) ENGINE=InnoDB;
    """
    r = run_ddl(ddl)
    assert r.success is False
    blob = " ".join(r.ddl_errors).lower()
    # the real MySQL message — not a generic "generation failed"
    assert "3780" in blob or "incompatible" in blob
    assert "fk_child_parent" in blob
    assert "child_all" in blob  # we also record which statement failed


def test_duplicate_constraint_name_reports_real_mysql_error(run_ddl):
    ddl = """
    CREATE TABLE a_all (id INT PRIMARY KEY) ENGINE=InnoDB;
    CREATE TABLE b_all (
      id INT PRIMARY KEY, a1 INT, a2 INT,
      CONSTRAINT fk_dup FOREIGN KEY (a1) REFERENCES a_all (id),
      CONSTRAINT fk_dup FOREIGN KEY (a2) REFERENCES a_all (id)
    ) ENGINE=InnoDB;
    """
    r = run_ddl(ddl)
    assert r.success is False
    blob = " ".join(r.ddl_errors).lower()
    assert "1061" in blob or "duplicate" in blob
    assert "fk_dup" in blob


def test_reserved_word_column_reports_syntax_error(run_ddl):
    r = run_ddl("CREATE TABLE t_all (id INT PRIMARY KEY, `order` INT, `order` INT) ENGINE=InnoDB;")
    # duplicate column via a reserved word — engine error, captured verbatim
    assert r.success is False
    blob = " ".join(r.ddl_errors).lower()
    assert "1060" in blob or "duplicate column" in blob

    r2 = run_ddl("CREATE TABLE t2_all (id INT PRIMARY KEY, order INT) ENGINE=InnoDB;")
    assert r2.success is False
    assert "1064" in " ".join(r2.ddl_errors)  # unquoted reserved word → parse error


def test_invalid_default_reports_error(run_ddl):
    """STRICT_ALL_TABLES is forced on, so a bad column default is a hard error."""
    r = run_ddl("CREATE TABLE d_all (id INT PRIMARY KEY, qty INT DEFAULT 'abc') ENGINE=InnoDB;")
    assert r.success is False
    blob = " ".join(r.ddl_errors).lower()
    assert "1067" in blob or "invalid default" in blob


def test_multiple_engine_errors_all_captured(run_ddl):
    """Execution is best-effort: every failing statement is reported, not just
    the first."""
    ddl = """
    CREATE TABLE ok_all (id INT PRIMARY KEY, created_on DATETIME, modified_on DATETIME) ENGINE=InnoDB;
    CREATE TABLE bad1_all (id INT PRIMARY KEY, qty INT DEFAULT 'abc') ENGINE=InnoDB;
    CREATE TABLE bad2_all (id INT PRIMARY KEY, x INT, x INT) ENGINE=InnoDB;
    """
    r = run_ddl(ddl)
    assert len(r.ddl_errors) >= 2
    assert r.statements_failed >= 2
    assert "ok_all" in r.tables_created  # the good one still landed


# ── enterprise checks: engine ─────────────────────────────────────
# Empirical basis: 96.9% of reference-dump tables are InnoDB; the 40 MyISAM
# tables in the corpus are the smell this reproduces.

def test_non_innodb_table_flagged_as_error(run_ddl):
    r = run_ddl(
        "CREATE TABLE legacy_all (id INT PRIMARY KEY, created_on DATETIME, modified_on DATETIME) "
        "ENGINE=MyISAM DEFAULT CHARSET=utf8mb4;"
    )
    cats = {(i.severity, i.category) for i in r.issues}
    assert ("error", "non-innodb") in cats


def test_all_innodb_has_no_engine_issue(run_ddl):
    r = run_ddl(
        "CREATE TABLE modern_all (id INT PRIMARY KEY, created_on DATETIME, modified_on DATETIME) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )
    assert not any(i.category == "non-innodb" for i in r.issues)


# ── enterprise checks: charset ────────────────────────────────────
# Empirical basis: 79.4% of reference dumps mix >1 charset across tables
# (e.g. tfyjqkpt_security.sql interleaves latin1, utf8mb4 and utf8mb3);
# utf8mb4 is only 34.6% of tables.

def test_mixed_and_legacy_charset_flagged(run_ddl):
    ddl = """
    CREATE TABLE u_all (id INT PRIMARY KEY, created_on DATETIME, modified_on DATETIME)
      ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    CREATE TABLE legacy_core_all (id INT PRIMARY KEY, created_on DATETIME, modified_on DATETIME)
      ENGINE=InnoDB DEFAULT CHARSET=latin1;
    """
    r = run_ddl(ddl)
    cats = {i.category for i in r.issues}
    assert "legacy-charset" in cats
    assert "charset-inconsistency" in cats


def test_consistent_utf8mb4_has_no_charset_issue(run_ddl):
    ddl = """
    CREATE TABLE a_all (id INT PRIMARY KEY, created_on DATETIME, modified_on DATETIME)
      ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    CREATE TABLE b_all (id INT PRIMARY KEY, created_on DATETIME, modified_on DATETIME)
      ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """
    r = run_ddl(ddl)
    assert not any(i.category in ("legacy-charset", "charset-inconsistency") for i in r.issues)


# ── enterprise checks: FK referential action ──────────────────────
# Relationship-aware suggestions per PostgreSQL's documented decision
# framework (docs/enterprise_standards_spec.md §1.1/§2.1) — not a single
# blanket "use CASCADE" nudge calibrated to how often real dumps bother.

def test_fk_left_at_implicit_restrict_is_advisory(run_ddl):
    ddl = """
    CREATE TABLE m1_all (id INT PRIMARY KEY, created_on DATETIME, modified_on DATETIME)
      ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    CREATE TABLE m2_all (
      id INT PRIMARY KEY, m1_id INT,
      created_on DATETIME, modified_on DATETIME,
      KEY k_m1 (m1_id),
      CONSTRAINT fk_m2_m1 FOREIGN KEY (m1_id) REFERENCES m1_all (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    hits = [i for i in r.issues if i.category == "fk-no-referential-action"]
    assert hits and hits[0].severity == "advisory"
    assert "fk_m2_m1" in hits[0].message


def test_fk_with_explicit_cascade_is_clean(run_ddl):
    ddl = """
    CREATE TABLE budget_header_all (id INT PRIMARY KEY, created_on DATETIME, modified_on DATETIME)
      ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    CREATE TABLE budget_details_all (
      id INT PRIMARY KEY, bud_id INT,
      created_on DATETIME, modified_on DATETIME,
      KEY k_bud (bud_id),
      CONSTRAINT budget_details_all_ibfk_1 FOREIGN KEY (bud_id)
        REFERENCES budget_header_all (id) ON DELETE CASCADE ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    assert not any(i.category == "fk-no-referential-action" for i in r.issues)


def test_owned_child_fk_suggests_cascade(run_ddl):
    ddl = """
    CREATE TABLE invoice_header_all (id INT AUTO_INCREMENT PRIMARY KEY, created_on DATETIME, modified_on DATETIME)
      ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    CREATE TABLE invoice_details_all (
      id INT AUTO_INCREMENT PRIMARY KEY, invoice_id INT NOT NULL,
      created_on DATETIME, modified_on DATETIME,
      KEY k_inv (invoice_id),
      CONSTRAINT fk_invdet_inv FOREIGN KEY (invoice_id) REFERENCES invoice_header_all (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    hits = [i for i in r.issues if i.category == "fk-no-referential-action"]
    assert hits
    assert "ON DELETE CASCADE ON UPDATE CASCADE" in hits[0].message
    assert "component" in hits[0].message.lower()


def test_independent_entities_fk_suggests_restrict(run_ddl):
    ddl = """
    CREATE TABLE warehouse_header_all (id INT AUTO_INCREMENT PRIMARY KEY, created_on DATETIME, modified_on DATETIME)
      ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    CREATE TABLE shipment_header_all (
      id INT AUTO_INCREMENT PRIMARY KEY, warehouse_id INT NOT NULL,
      created_on DATETIME, modified_on DATETIME,
      KEY k_wh (warehouse_id),
      CONSTRAINT fk_ship_wh FOREIGN KEY (warehouse_id) REFERENCES warehouse_header_all (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    hits = [i for i in r.issues if i.category == "fk-no-referential-action"]
    assert hits
    assert "ON DELETE RESTRICT ON UPDATE CASCADE" in hits[0].message
    assert "independent" in hits[0].message.lower()


def test_soft_delete_parent_fk_suggests_restrict_never_cascade(run_ddl):
    ddl = """
    CREATE TABLE customer_header_all (
      id INT AUTO_INCREMENT PRIMARY KEY, deleted_at DATETIME NULL,
      created_on DATETIME, modified_on DATETIME
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    CREATE TABLE order_header_all (
      id INT AUTO_INCREMENT PRIMARY KEY, customer_id INT NOT NULL,
      created_on DATETIME, modified_on DATETIME,
      KEY k_cust (customer_id),
      CONSTRAINT fk_order_cust FOREIGN KEY (customer_id) REFERENCES customer_header_all (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    hits = [i for i in r.issues if i.category == "fk-no-referential-action"]
    assert hits
    assert "ON DELETE RESTRICT" in hits[0].message
    assert "soft-deletable" in hits[0].message.lower()
    assert "deleted_at" in hits[0].message


def test_nullable_optional_fk_suggests_set_null(run_ddl):
    ddl = """
    CREATE TABLE manager_header_all (id INT AUTO_INCREMENT PRIMARY KEY, created_on DATETIME, modified_on DATETIME)
      ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    CREATE TABLE employee_header_all (
      id INT AUTO_INCREMENT PRIMARY KEY, manager_id INT NULL,
      created_on DATETIME, modified_on DATETIME,
      KEY k_mgr (manager_id),
      CONSTRAINT fk_emp_mgr FOREIGN KEY (manager_id) REFERENCES manager_header_all (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    hits = [i for i in r.issues if i.category == "fk-no-referential-action"]
    assert hits
    assert "ON DELETE SET NULL" in hits[0].message
    assert "optional" in hits[0].message.lower()


# ── enterprise checks: soft-delete + CASCADE conflict ──────────────
# A distinct, more specific finding than "left implicit" above: here the
# action IS explicit, and it's the wrong one — CASCADE never fires on the
# UPDATE a soft delete actually performs (docs/enterprise_standards_spec.md §1.1/§1.6).

def test_explicit_cascade_on_soft_delete_parent_flagged(run_ddl):
    ddl = """
    CREATE TABLE account_header_all (
      id INT AUTO_INCREMENT PRIMARY KEY, is_deleted TINYINT NOT NULL DEFAULT 0,
      created_on DATETIME, modified_on DATETIME
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    CREATE TABLE transaction_header_all (
      id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL,
      created_on DATETIME, modified_on DATETIME,
      KEY k_acct (account_id),
      CONSTRAINT fk_txn_acct FOREIGN KEY (account_id) REFERENCES account_header_all (id)
        ON DELETE CASCADE ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    hits = [i for i in r.issues if i.category == "soft-delete-cascade-conflict"]
    assert hits
    assert hits[0].severity == "advisory"
    assert "is_deleted" in hits[0].message
    assert hits[0].table == "account_header_all"


def test_cascade_on_non_soft_delete_parent_not_flagged(run_ddl):
    ddl = """
    CREATE TABLE order_header_all (id INT AUTO_INCREMENT PRIMARY KEY, created_on DATETIME, modified_on DATETIME)
      ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    CREATE TABLE order_details_all (
      id INT AUTO_INCREMENT PRIMARY KEY, order_id INT NOT NULL,
      created_on DATETIME, modified_on DATETIME,
      KEY k_ord (order_id),
      CONSTRAINT fk_orddet_ord FOREIGN KEY (order_id) REFERENCES order_header_all (id)
        ON DELETE CASCADE ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    assert not any(i.category == "soft-delete-cascade-conflict" for i in r.issues)


def test_restrict_on_soft_delete_parent_not_flagged_as_conflict(run_ddl):
    ddl = """
    CREATE TABLE account_header_all (
      id INT AUTO_INCREMENT PRIMARY KEY, deleted_at DATETIME NULL,
      created_on DATETIME, modified_on DATETIME
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    CREATE TABLE transaction_header_all (
      id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL,
      created_on DATETIME, modified_on DATETIME,
      KEY k_acct (account_id),
      CONSTRAINT fk_txn_acct FOREIGN KEY (account_id) REFERENCES account_header_all (id)
        ON DELETE RESTRICT ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    assert not any(i.category == "soft-delete-cascade-conflict" for i in r.issues)


# ── enterprise checks: multi-FK table indexing ───────────────────
# Empirical basis: 33/33 (100%) reference-dump tables with >1 FK column index
# every FK column. tfyjqkpt_kaizen.sql tables carry req_id/po_id/ven_id/dept_id/
# cat_id/eq_id, each attached to a KEY.

def test_multi_fk_table_with_unindexed_fk_column_is_advisory(run_ddl):
    ddl = """
    SET FOREIGN_KEY_CHECKS = 0;
    CREATE TABLE requisition_details_all (
      id INT AUTO_INCREMENT PRIMARY KEY,
      req_id INT NOT NULL COMMENT 'FK -> requisition_header_all',
      ven_id INT NOT NULL COMMENT 'FK -> vendor_header_all',
      dept_id INT NOT NULL COMMENT 'FK -> department_header_all',
      qty INT NOT NULL,
      created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL,
      KEY idx_reqdet_req (req_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    hits = [i for i in r.issues if i.category == "multi-fk-missing-index"]
    assert hits, [i.category for i in r.issues]
    assert "ven_id" in hits[0].message and "dept_id" in hits[0].message
    assert "req_id" not in hits[0].message  # that one is indexed


def test_multi_fk_table_with_every_fk_indexed_is_clean(run_ddl):
    ddl = """
    SET FOREIGN_KEY_CHECKS = 0;
    CREATE TABLE po_details_all (
      id INT AUTO_INCREMENT PRIMARY KEY,
      po_id INT NOT NULL, ven_id INT NOT NULL, cat_id INT NOT NULL,
      created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL,
      KEY idx_podet_po (po_id), KEY idx_podet_ven (ven_id), KEY idx_podet_cat (cat_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    assert not any(i.category == "multi-fk-missing-index" for i in r.issues)


# ── enterprise checks: audit timestamps ──────────────────────────
# Empirical basis: only 32.7% of reference base tables carry any timestamp
# (hence advisory, not error). Dominant names: created_on / modified_on.

def test_base_table_missing_timestamps_is_advisory(run_ddl):
    r = run_ddl(
        "CREATE TABLE vendor_header_all (id INT AUTO_INCREMENT PRIMARY KEY, "
        "name VARCHAR(120) NOT NULL, status TINYINT NOT NULL DEFAULT 1) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
    )
    hits = [i for i in r.issues if i.category == "missing-timestamps"]
    assert hits and hits[0].severity == "advisory"
    assert hits[0].table == "vendor_header_all"


def test_base_table_with_created_on_modified_on_is_clean(run_ddl):
    r = run_ddl(
        "CREATE TABLE vendor2_header_all (id INT AUTO_INCREMENT PRIMARY KEY, "
        "name VARCHAR(120) NOT NULL, created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
    )
    assert not any(i.category == "missing-timestamps" for i in r.issues)


def test_pure_junction_table_is_exempt_from_timestamp_advisory(run_ddl):
    """A student↔class link table with no timestamps must NOT be nagged —
    the exemption the spec calls for."""
    ddl = """
    SET FOREIGN_KEY_CHECKS = 0;
    CREATE TABLE student_class_map_all (
      student_id INT NOT NULL,
      class_id INT NOT NULL,
      PRIMARY KEY (student_id, class_id),
      KEY idx_scm_class (class_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    assert not any(i.category == "missing-timestamps" for i in r.issues)


# ── enterprise checks: redundant index ───────────────────────────

def test_redundant_prefix_index_is_advisory(run_ddl):
    ddl = """
    CREATE TABLE w_all (
      id INT AUTO_INCREMENT PRIMARY KEY,
      a INT, b INT,
      created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL,
      KEY k_a (a),
      KEY k_ab (a, b)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    hits = [i for i in r.issues if i.category == "redundant-index"]
    assert hits
    assert hits[0].object_name == "k_a"


def test_distinct_indexes_are_clean(run_ddl):
    ddl = """
    CREATE TABLE w2_all (
      id INT AUTO_INCREMENT PRIMARY KEY,
      a INT, b INT,
      created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL,
      KEY k_a (a),
      KEY k_b (b)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    r = run_ddl(ddl)
    assert not any(i.category == "redundant-index" for i in r.issues)


# ─────────────────── multi-schema validation (decomposed projects) ────────
# docs/enterprise_standards_spec.md §2.4/§6. execute_and_validate above is
# completely untouched — these exercise the additive multi-schema path.

@pytest.fixture
def multi_schema_enabled(_mysql_dsn, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_ENABLED", True)
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_DSN", _mysql_dsn)
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_USE_TESTCONTAINER", False)
    monkeypatch.setenv("MYSQL_EXEC_VALIDATION_DSN", _mysql_dsn)
    return _mysql_dsn


def test_two_independent_clean_schemas_both_succeed(multi_schema_enabled):
    identity_ddl = """
    CREATE TABLE customer_header_all (
      id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(120) NOT NULL,
      created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """
    billing_ddl = """
    CREATE TABLE plan_header_all (
      id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(80) NOT NULL,
      created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """
    results = execute_and_validate_schemas({"identity": identity_ddl, "billing": billing_ddl})

    assert set(results) == {"identity", "billing"}
    for name in ("identity", "billing"):
        r = results[name]
        assert r.skipped is False
        assert r.success is True, r.ddl_errors
        assert not any(i.category == "cross-schema-fk-violation" for i in r.issues)
    assert results["identity"].tables_created == ["customer_header_all"]
    assert results["billing"].tables_created == ["plan_header_all"]


def test_ddl_error_in_one_schema_does_not_affect_the_other(multi_schema_enabled):
    broken_ddl = "CREATE TABLE bad_all (id INT PRIMARY KEY, x INT, x INT) ENGINE=InnoDB;"
    clean_ddl = """
    CREATE TABLE ok_header_all (
      id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(80) NOT NULL,
      created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """
    results = execute_and_validate_schemas({"broken": broken_ddl, "clean": clean_ddl})

    assert results["broken"].success is False
    assert results["broken"].ddl_errors
    assert results["clean"].success is True
    assert results["clean"].tables_created == ["ok_header_all"]


def test_check_cross_schema_fk_violations_detects_a_real_cross_database_fk(multi_schema_enabled):
    """Direct unit test of the detection query itself, using two real,
    fixed-name databases and a genuine same-server cross-database FK — the
    defense-in-depth case split_ddl_by_schema is meant to prevent from ever
    reaching here, but this proves the check catches it if something does."""
    import pymysql
    from app.services import mysql_execution_validator as mev

    base = mev._parse_dsn(multi_schema_enabled)
    admin = {k: v for k, v in base.items() if k != "database"}
    db_a, db_b = "execval_test_xfk_a", "execval_test_xfk_b"

    conn = pymysql.connect(**admin)
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            for db in (db_a, db_b):
                cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
                cur.execute(f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            cur.execute(f"USE `{db_a}`")
            cur.execute(
                "CREATE TABLE parent_all (id INT PRIMARY KEY) ENGINE=InnoDB "
                "DEFAULT CHARSET=utf8mb4;"
            )
            cur.execute(f"USE `{db_b}`")
            cur.execute(
                f"CREATE TABLE child_all (id INT PRIMARY KEY, parent_id INT, "
                f"KEY k_parent (parent_id), "
                f"CONSTRAINT fk_child_parent FOREIGN KEY (parent_id) "
                f"REFERENCES `{db_a}`.parent_all(id)"
                f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
            )
        conn.commit()

        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            issues = mev._check_cross_schema_fk_violations(cur, [db_a, db_b])

        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert issues[0].category == "cross-schema-fk-violation"
        assert issues[0].table == f"{db_b}.child_all"
        assert db_a in issues[0].message and "parent_all" in issues[0].message
    finally:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            for db in (db_a, db_b):
                cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
        conn.close()


def test_check_cross_schema_fk_violations_clean_when_no_cross_refs(multi_schema_enabled):
    import pymysql
    from app.services import mysql_execution_validator as mev

    base = mev._parse_dsn(multi_schema_enabled)
    admin = {k: v for k, v in base.items() if k != "database"}
    db_a, db_b = "execval_test_noxfk_a", "execval_test_noxfk_b"

    conn = pymysql.connect(**admin)
    try:
        with conn.cursor() as cur:
            for db in (db_a, db_b):
                cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
                cur.execute(f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4")
            cur.execute(f"USE `{db_a}`")
            cur.execute("CREATE TABLE a_all (id INT PRIMARY KEY) ENGINE=InnoDB;")
            cur.execute(f"USE `{db_b}`")
            cur.execute("CREATE TABLE b_all (id INT PRIMARY KEY) ENGINE=InnoDB;")
        conn.commit()

        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            issues = mev._check_cross_schema_fk_violations(cur, [db_a, db_b])
        assert issues == []
    finally:
        with conn.cursor() as cur:
            for db in (db_a, db_b):
                cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
        conn.commit()
        conn.close()
