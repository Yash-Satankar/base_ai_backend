"""
Tests for SchemaValidator — focused on the production-hardening checks added
alongside rules 21 (InnoDB), 22 (utf8mb4), 23 (DATETIME) and the reworked
three-layer data-preservation nudge.
"""

import pytest

from app.validators.schema_validator import SchemaValidator, rule_count


GOOD_SCHEMA = """
CREATE TABLE unique_id_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  table_name VARCHAR(100) NOT NULL COMMENT 'target table',
  prefix VARCHAR(20) NOT NULL COMMENT 'business id prefix',
  last_id VARCHAR(15) NOT NULL DEFAULT '00000' COMMENT 'last issued',
  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE employee_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT 'surrogate key',
  employee_id VARCHAR(20) NOT NULL UNIQUE COMMENT 'business id EMP00001',
  full_name VARCHAR(150) NOT NULL COMMENT 'employee name',
  mobile_no VARCHAR(15) NOT NULL COMMENT 'contact',
  old_mobile_no VARCHAR(15) DEFAULT NULL COMMENT 'previous contact (layer 2)',
  designation VARCHAR(80) NOT NULL COMMENT 'role',
  grade_band VARCHAR(20) NOT NULL COMMENT 'pay band code',
  old_grade_band VARCHAR(20) DEFAULT NULL COMMENT 'previous band (layer 2)',
  date_of_birth DATE DEFAULT NULL COMMENT 'dob is a pure calendar date',
  joining_date DATE NOT NULL COMMENT 'pure calendar date',
  added_by INT NOT NULL COMMENT 'creator user id',
  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive,3=resigned,4=terminated',
  created_on DATETIME NOT NULL COMMENT 'created',
  modified_on DATETIME NOT NULL COMMENT 'modified'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE employee_archive_all (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT 'surrogate key',
  employee_id VARCHAR(20) NOT NULL COMMENT 'business id copied from header',
  full_name VARCHAR(150) NOT NULL COMMENT 'employee name',
  mobile_no VARCHAR(15) NOT NULL COMMENT 'contact',
  designation VARCHAR(80) NOT NULL COMMENT 'role',
  grade_band VARCHAR(20) NOT NULL COMMENT 'pay band code',
  archived_on DATETIME NOT NULL COMMENT 'snapshot time',
  archived_by INT NOT NULL COMMENT 'who archived',
  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',
  created_on DATETIME NOT NULL COMMENT 'created',
  modified_on DATETIME NOT NULL COMMENT 'modified'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE employee_life_cycle_all (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT 'surrogate key',
  employee_id VARCHAR(20) NOT NULL COMMENT 'entity business id',
  previous_status INT NOT NULL COMMENT '1=active,2=inactive,3=resigned',
  new_status INT NOT NULL COMMENT '1=active,2=inactive,3=resigned',
  reason VARCHAR(500) COMMENT 'why the transition happened',
  changed_by INT NOT NULL COMMENT 'actor user id',
  changed_on DATETIME NOT NULL COMMENT 'transition time',
  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',
  created_on DATETIME NOT NULL COMMENT 'created',
  modified_on DATETIME NOT NULL COMMENT 'modified'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

BAD_SCHEMA = """
CREATE TABLE payment_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  payment_id VARCHAR(20) NOT NULL,
  amount FLOAT NOT NULL,
  paid_amount DOUBLE NOT NULL,
  status INT NOT NULL DEFAULT 1 COMMENT '1=active',
  created_on DATE NOT NULL,
  modified_on DATE NOT NULL
) ENGINE=MyISAM DEFAULT CHARSET=latin1;
"""


@pytest.fixture(scope="module")
def validator():
    return SchemaValidator()


def _rule_ids(result):
    return {i.rule_id for i in result.issues}


# ── Good schema ───────────────────────────────────────────────────

def test_good_schema_passes_with_no_hardening_violations(validator):
    r = validator.validate(GOOD_SCHEMA)
    assert r.passed, r.summary
    assert r.critical_issues == 0, [i.issue for i in r.issues if i.severity == "critical"]
    offending = {21, 22, 23} & _rule_ids(r)
    assert not offending, [i.issue for i in r.issues if i.rule_id in (21, 22, 23)]


def test_good_schema_has_no_preservation_nudge_for_full_trio(validator):
    r = validator.validate(GOOD_SCHEMA)
    # employee_* has header + archive + life_cycle → no rule 3/4 nudge
    assert 3 not in _rule_ids(r)
    assert 4 not in _rule_ids(r)


# ── Bad schema ───────────────────────────────────────────────────

def test_myisam_is_a_critical_engine_violation(validator):
    r = validator.validate(BAD_SCHEMA)
    engine_issues = [i for i in r.issues if i.rule_id == 21]
    assert engine_issues
    assert any(i.severity == "critical" and "myisam" in i.issue.lower() for i in engine_issues)


def test_latin1_is_a_charset_violation(validator):
    r = validator.validate(BAD_SCHEMA)
    cs_issues = [i for i in r.issues if i.rule_id == 22]
    assert cs_issues
    assert any("latin1" in i.issue.lower() for i in cs_issues)
    assert any(i.severity == "high" for i in cs_issues)


def test_date_typed_timestamp_is_flagged(validator):
    r = validator.validate(BAD_SCHEMA)
    temporal = [i for i in r.issues if i.rule_id == 23]
    flagged = {i.issue.split("'")[1] for i in temporal}
    assert "created_on" in flagged and "modified_on" in flagged


def test_float_money_still_flagged(validator):
    r = validator.validate(BAD_SCHEMA)
    assert any(i.rule_id == 29 for i in r.issues)


def test_bad_schema_missing_archive_and_lifecycle_nudges(validator):
    r = validator.validate(BAD_SCHEMA)
    ids = _rule_ids(r)
    assert 3 in ids  # no payment_archive_all
    assert 4 in ids  # no payment_life_cycle_all
    assert all(
        i.severity == "low" for i in r.issues if i.rule_id in (3, 4)
    )


# ── Parser + summary ─────────────────────────────────────────────

def test_iter_create_tables_handles_nested_parens(validator):
    sql = (
        "CREATE TABLE t_all (\n"
        "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
        "  amt DECIMAL(10,2) NOT NULL,\n"
        "  KEY idx_t_all_id (id)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
    )
    parsed = list(validator._iter_create_tables(sql))
    assert len(parsed) == 1
    name, body, tail = parsed[0]
    assert name == "t_all"
    assert "DECIMAL(10,2)" in body
    assert "engine=innodb" in tail.lower()


def test_summary_reports_live_rule_count(validator):
    r = validator.validate(GOOD_SCHEMA)
    assert f"Checked against {rule_count()} rules" in r.summary
    assert rule_count() >= 98
