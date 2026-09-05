"""
Tests for SchemaValidator — focused on the production-hardening checks added
alongside rules 21 (InnoDB), 22 (utf8mb4), 23 (DATETIME) and the reworked
three-layer data-preservation nudge.
"""

import pytest

from app.validators.schema_validator import SchemaValidator, rule_count, is_high_criticality_domain


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
    # high_criticality=True: HR/employee data is a plausible in-scope case,
    # and the point of this test is the *complete-trio* exemption, which
    # only matters once the nudge is in scope at all — see
    # docs/enterprise_standards_spec.md §2.5.
    r = validator.validate(GOOD_SCHEMA, high_criticality=True)
    # employee_* has header + archive + life_cycle → no rule 3/4 nudge
    assert 3 not in _rule_ids(r)
    assert 4 not in _rule_ids(r)


def test_preservation_nudge_is_exempt_by_default_outside_high_criticality_domains(validator):
    """rules 3/4 are scoped to financial/regulated/high-criticality domains
    (docs/enterprise_standards_spec.md §2.1/§2.5) — no researched source
    supports mandating a paired history table for every mutable business
    entity regardless of domain. BAD_SCHEMA's payment_header_all has no
    archive/life_cycle companion, but with the default high_criticality=False
    the nudge must not fire at all."""
    r = validator.validate(BAD_SCHEMA)
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


def test_bad_schema_missing_archive_and_lifecycle_nudges_in_financial_domain(validator):
    # payment_header_all is exactly the kind of table this nudge is meant
    # for once it's in scope — high_criticality=True represents a
    # financial-domain caller (see is_high_criticality_domain).
    r = validator.validate(BAD_SCHEMA, high_criticality=True)
    ids = _rule_ids(r)
    assert 3 in ids  # no payment_archive_all
    assert 4 in ids  # no payment_life_cycle_all
    assert all(
        i.severity == "low" for i in r.issues if i.rule_id in (3, 4)
    )


# ── is_high_criticality_domain classifier ────────────────────────

@pytest.mark.parametrize("domain", [
    "financial ledger", "Banking", "insurance claims", "healthcare EHR",
    "hospital management", "government payroll", "legal case management",
])
def test_high_criticality_keywords_detected(domain):
    assert is_high_criticality_domain(domain) is True


@pytest.mark.parametrize("domain", [
    "logistics", "multi-tenant SaaS", "e-commerce", "warehouse management",
    "", None,
])
def test_non_high_criticality_domains_not_flagged(domain):
    assert is_high_criticality_domain(domain) is False


def test_gst_required_flags_high_criticality_regardless_of_domain():
    assert is_high_criticality_domain("logistics", gst_required=True) is True


def test_named_compliance_requirement_flags_high_criticality():
    assert is_high_criticality_domain("e-commerce", compliance_requirements=["PCI-DSS"]) is True
    assert is_high_criticality_domain("e-commerce", compliance_requirements=[]) is False


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
