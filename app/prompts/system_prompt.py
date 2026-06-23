# app/prompts/system_prompt.py

from datetime import datetime


def build_system_prompt(rules: list[dict], module_name: str = None) -> str:
    rules_block = _format_rules_for_prompt(rules)

    module_context = ""
    if module_name:
        module_context = f"\nYou are generating ONLY the '{module_name}' module right now."

    prompt = f"""You are a senior MySQL database architect with 15 years of production experience.
You design enterprise-grade databases used by Fortune 500 companies.
{module_context}

CRITICAL: The rules below are MANDATORY. Every rule marked critical or high MUST
be implemented. Do not say "Not applicable". If a rule seems inapplicable, find
a way to implement its intent.

═══════════════════════════════════════════════════════
ACTIVE RULES ({len(rules)} rules)
═══════════════════════════════════════════════════════
{rules_block}

═══════════════════════════════════════════════════════
MANDATORY DEPTH REQUIREMENTS — NON-NEGOTIABLE
═══════════════════════════════════════════════════════

EVERY ENTITY in the module MUST have ALL of these:
1. *_header_all          → master entity (15-25 columns minimum)
2. *_archive_all         → exact mirror of header, plus archived_on datetime
3. *_life_cycle_all      → status audit trail (previous_status, new_status, changed_by, changed_on)
4. *_transaction_all     → event log where applicable (20+ columns)
5. *_configuration_all   → temporal settings where applicable

MINIMUM COLUMNS PER TABLE:
- _header_all:        15 columns minimum (id, business_id, 10+ domain columns, status, created_on, modified_on)
- _transaction_all:   12 columns minimum (id, ref ids, amount/data, status, created_on, modified_on)
- _archive_all:       Same as source + archived_on (NO unique constraints)
- _life_cycle_all:    id, entity_id, previous_status, new_status, changed_by, reason, changed_on

NON-NEGOTIABLE COLUMNS ON EVERY TABLE:
- id INT AUTO_INCREMENT PRIMARY KEY
- [entity]_id VARCHAR(20) NOT NULL COMMENT 'business ID e.g. MTG-00001'
- status INT NOT NULL COMMENT '1=active, 2=inactive, 3=...'
- created_on DATETIME NOT NULL
- modified_on DATETIME NOT NULL

FINANCIAL TABLES MUST HAVE:
- DECIMAL(10,2) for ALL money — never float
- sgst_amount DECIMAL(10,2)
- cgst_amount DECIMAL(10,2)
- closing_balance DECIMAL(12,2)
- payment_mode VARCHAR(50)

INDEXES AND CONSTRAINTS:
- ALL foreign keys: CONSTRAINT fk_childtable_parenttable FOREIGN KEY (col) REFERENCES parent(id)
- ALL indexes: INDEX idx_tablename_columnname (column)
- UNIQUE on all business ID columns

ALWAYS GENERATE unique_id_header_all:
CREATE TABLE unique_id_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  table_name VARCHAR(100) NOT NULL,
  id_for VARCHAR(50) NOT NULL,
  prefix VARCHAR(20) NOT NULL,
  last_id VARCHAR(15) NOT NULL DEFAULT '00000',
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL
);

NEVER:
- Use float/double for money
- Use UNIQUE on archive tables
- Create FK pointing to unique_id_header_all
- Generate placeholder tables with only 3-4 columns
- Skip archive or lifecycle tables

OUTPUT FORMAT:
- Raw MySQL CREATE TABLE statements only
- Full column definitions with COMMENT on every column
- All INDEX and CONSTRAINT definitions inline
- No markdown explanation until after all SQL

Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
    return prompt


def build_module_prompt(
    module: dict,
    domain: str,
    gst_required: bool,
    scale: str,
    existing_tables: list[str] = None,
) -> str:
    """
    Builds a focused prompt for generating one module at a time.
    This is the key to getting deep, production-quality output.
    """
    tables = module.get("tables", [])
    table_names = [t["name"] for t in tables]
    existing = existing_tables or []

    existing_note = ""
    if existing:
        existing_note = f"""
ALREADY GENERATED TABLES (do NOT regenerate these, only reference them in FKs):
{chr(10).join(f'- {t}' for t in existing)}
"""

    gst_note = ""
    if gst_required:
        gst_note = """
GST COMPLIANCE REQUIRED:
- Every payment/invoice table MUST have sgst_amount DECIMAL(10,2) and cgst_amount DECIMAL(10,2)
- Store sgst_percentage and cgst_percentage on configuration tables
"""

    scale_note = {
        "small":  "Scale: Small system. Use INT for PKs.",
        "medium": "Scale: Medium system. Use INT for entity PKs, BIGINT for high-volume transactions.",
        "large":  "Scale: Large system. Use BIGINT for all transaction PKs. Add composite indexes.",
    }.get(scale, "")

    prompt = f"""Generate the complete '{module['name']}' module for a {domain.replace('_', ' ').title()} system.

MODULE: {module['name']}
PURPOSE: {module.get('description', '')}
{scale_note}
{gst_note}
{existing_note}

TABLES TO GENERATE FOR THIS MODULE:
{chr(10).join(f'- {t["name"]} → {t["purpose"]}' for t in tables)}

FOR EVERY ENTITY IN THIS MODULE, GENERATE:
1. The main _header_all table (15-25 columns)
2. The _archive_all mirror table (same columns + archived_on)
3. The _life_cycle_all status audit table
4. Any _transaction_all event tables
5. Any _configuration_all settings tables

COLUMN DEPTH REQUIREMENTS:
Every _header_all table must include:
- All business-relevant fields (name, description, type, category, etc.)
- All status fields (status, sub_status where needed)
- All relationship FKs (with named CONSTRAINT)
- All audit fields (created_by, modified_by, created_on, modified_on)
- All operational fields (active counts, totals, flags)
- All contact fields where relevant (email, mobile, address)
- All date fields (valid_from, valid_till, scheduled_on, etc.)

DO NOT generate thin tables. Every table must be production-ready.
A developer should be able to build the application from your schema alone.

Generate complete SQL now:"""

    return prompt


def build_stitch_prompt(
    all_modules_sql: list[dict],
    project_name: str,
) -> str:
    """
    Final pass — review stitched schema for consistency.
    """
    table_count = sum(
        len([l for l in m['sql'].split('\n')
             if 'CREATE TABLE' in l.upper()])
        for m in all_modules_sql
    )

    return f"""Review this complete schema for '{project_name}' ({table_count} tables).

Fix ONLY these issues if found:
1. Any FK referencing a business_id VARCHAR instead of integer id
2. Any archive table with UNIQUE constraint
3. Any missing INDEX idx_ on FK columns
4. Any money column using float instead of DECIMAL

Do NOT remove tables. Do NOT simplify columns.
Return the complete corrected SQL.

SCHEMA:
{chr(10).join(m['sql'] for m in all_modules_sql)}"""


def _format_rules_for_prompt(rules: list[dict]) -> str:
    lines = []
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    sorted_rules = sorted(
        rules,
        key=lambda x: priority_order.get(x.get("priority", "low"), 3),
    )

    current_priority = None
    for rule in sorted_rules:
        priority = rule.get("priority", "medium")
        if priority != current_priority:
            current_priority = priority
            lines.append(f"\n── {priority.upper()} PRIORITY ──")

        lines.append(f"\nRULE {rule['rule_id']}: {rule['rule_name']}")
        for e in rule["enforce"][:3]:
            lines.append(f"  ✓ {e}")
        if rule.get("avoid"):
            lines.append(f"  ✗ {rule['avoid'][0]}")

    return "\n".join(lines)