"""
Tests for cross-schema DDL handling (app/services/schema_decomposition.py)
— see docs/enterprise_standards_spec.md §2.3/§5. Pure text manipulation,
no MySQL needed.
"""

from app.services.schema_decomposition import split_ddl_by_schema


def _sql_part(module: str, sql: str, tables: list[str]) -> dict:
    return {"module": module, "sql": sql, "tables": tables}


IDENTITY_SQL = """
CREATE TABLE customer_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(120) NOT NULL,
  status INT NOT NULL DEFAULT 1,
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

BILLING_SQL = """
CREATE TABLE subscription_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  customer_id INT NOT NULL,
  plan_name VARCHAR(80) NOT NULL,
  status INT NOT NULL DEFAULT 1,
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL,
  KEY idx_sub_customer (customer_id),
  CONSTRAINT fk_sub_customer FOREIGN KEY (customer_id)
    REFERENCES customer_header_all (id) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE subscription_line_item_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  subscription_id INT NOT NULL,
  amount DECIMAL(10,2) NOT NULL,
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL,
  KEY idx_line_sub (subscription_id),
  CONSTRAINT fk_line_sub FOREIGN KEY (subscription_id)
    REFERENCES subscription_header_all (id) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def _base_parts():
    return [
        _sql_part("Identity", IDENTITY_SQL, ["customer_header_all"]),
        _sql_part("Billing", BILLING_SQL, ["subscription_header_all", "subscription_line_item_all"]),
    ]


def _module_schema_map():
    return {"Identity": "identity", "Billing": "billing"}


def test_returns_one_ddl_string_per_schema():
    result = split_ddl_by_schema(_base_parts(), "Test Project", _module_schema_map())
    assert set(result) == {"identity", "billing"}
    assert "customer_header_all" in result["identity"]
    assert "subscription_header_all" in result["billing"]
    # billing's own DDL never DEFINES customer_header_all — it only
    # documents the cross-schema reference to it (checked in a later test)
    assert "CREATE TABLE customer_header_all" not in result["billing"]


def test_cross_schema_fk_downgraded_to_commented_column():
    result = split_ddl_by_schema(_base_parts(), "Test Project", _module_schema_map())
    billing_ddl = result["billing"]

    assert "CONSTRAINT fk_sub_customer" not in billing_ddl
    assert "FOREIGN KEY (customer_id)" not in billing_ddl
    assert "customer_id INT NOT NULL COMMENT" in billing_ddl
    assert "References identity.customer_header_all(id)" in billing_ddl
    assert "cross-schema, no FK by design" in billing_ddl


def test_same_schema_fk_is_left_as_a_real_constraint():
    result = split_ddl_by_schema(_base_parts(), "Test Project", _module_schema_map())
    billing_ddl = result["billing"]

    # subscription_line_item_all -> subscription_header_all: both in 'billing'
    assert "CONSTRAINT fk_line_sub FOREIGN KEY (subscription_id)" in billing_ddl
    assert "REFERENCES subscription_header_all (id)" in billing_ddl


def test_every_table_gets_an_ownership_header_comment():
    result = split_ddl_by_schema(_base_parts(), "Test Project", _module_schema_map())
    assert result["identity"].count("-- Owned by: identity schema") == 1
    assert result["billing"].count("-- Owned by: billing schema") == 2


def test_existing_column_comment_is_replaced_not_duplicated():
    sql_with_comment = BILLING_SQL.replace(
        "customer_id INT NOT NULL,",
        "customer_id INT NOT NULL COMMENT 'the paying customer',",
    )
    parts = [
        _sql_part("Identity", IDENTITY_SQL, ["customer_header_all"]),
        _sql_part("Billing", sql_with_comment, ["subscription_header_all", "subscription_line_item_all"]),
    ]
    result = split_ddl_by_schema(parts, "Test Project", _module_schema_map())
    billing_ddl = result["billing"]

    assert billing_ddl.count("COMMENT") >= 1
    assert "the paying customer" not in billing_ddl
    assert "References identity.customer_header_all(id)" in billing_ddl
    # exactly one COMMENT on the customer_id line, not two stacked
    customer_line = next(l for l in billing_ddl.splitlines() if l.strip().startswith("customer_id"))
    assert customer_line.count("COMMENT") == 1
