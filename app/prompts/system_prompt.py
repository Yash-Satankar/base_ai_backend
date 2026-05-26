# app/prompts/system_prompt.py

from datetime import datetime


def build_system_prompt(rules: list[dict]) -> str:
    """
    Assembles the full system prompt by injecting
    the retrieved rules into the base prompt.
    """

    rules_block = _format_rules_for_prompt(rules)

    prompt = f"""You are an expert MySQL database schema architect.
Your job is to generate production-quality MySQL schema (CREATE TABLE statements).

CRITICAL: The rules below are NOT optional. Every rule marked critical or high 
MUST be implemented in the schema. Do not say "Not applicable" for critical/high rules.
If a rule seems inapplicable, find a way to incorporate its intent.

═══════════════════════════════════════════════════════
ACTIVE RULES — MUST FOLLOW ({len(rules)} rules)
═══════════════════════════════════════════════════════
{rules_block}

═══════════════════════════════════════════════════════
NON-NEGOTIABLE REQUIREMENTS — EVERY SCHEMA MUST HAVE
═══════════════════════════════════════════════════════

1. unique_id_header_all TABLE — always generate this:
   CREATE TABLE unique_id_header_all (
     id int AUTO_INCREMENT PRIMARY KEY,
     table_name varchar(100) NOT NULL,
     id_for varchar(50) NOT NULL,
     prefix varchar(20) NOT NULL,
     last_id varchar(15) NOT NULL,
     created_on datetime NOT NULL,
     modified_on datetime NOT NULL
   );

2. ALL FOREIGN KEYS must:
   - Reference the `id` column (int PK), not the business ID
   - Have named constraint: CONSTRAINT fk_childtable_parenttable

3. ALL INDEXES must be named:
   - INDEX idx_tablename_columnname (column)

4. ALL MONEY columns: DECIMAL(10,2) — never float or double

5. GST: always separate cgst_amount and sgst_amount columns

6. STATUS: always int with COMMENT '1=active,2=inactive,...'

7. ARCHIVE TABLES: never add UNIQUE constraints — they store historical duplicates

8. TIMESTAMPS: created_on and modified_on on EVERY table

# Add inside build_system_prompt, in the NON-NEGOTIABLE block:

9. unique_id_header_all is a REGISTRY table only.
   - NO other table should have a FOREIGN KEY pointing to it
   - It is written to when generating new IDs, nothing more

10. Every single table without exception needs a status column

11. EVERY payment/transaction table must have:
    - closing_balance DECIMAL(12,2)
    - cgst_amount DECIMAL(10,2)
    - sgst_amount DECIMAL(10,2)
    - payment_method VARCHAR(50)

12. fee_transaction_all and payment tables need status column too

NAMING:
- Master entity: *_header_all
- Events/ledger: *_transaction_all  
- Config: *_configuration_all
- History: *_archive_all
- Status audit: *_life_cycle_all

Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
    return prompt


def build_user_prompt(
    requirement: str,
    domain: str,
    additional_context: str = None,
) -> str:
    """
    Builds the user-facing prompt from their requirement.
    """
    context_block = ""
    if additional_context:
        context_block = f"""
Additional Context:
{additional_context}
"""

    prompt = f"""Generate a complete MySQL database schema for the following requirement:

DOMAIN: {domain.upper()}

REQUIREMENT:
{requirement}
{context_block}

Generate all necessary tables following the rules provided.
Include tables for: entities, transactions, configuration, audit trails,
and archive tables where appropriate.

Return complete CREATE TABLE statements ready to run in MySQL.
"""
    return prompt


def _format_rules_for_prompt(rules: list[dict]) -> str:
    """
    Format rules list into a readable block for the prompt.
    Keeps it concise — LLM doesn't need the full JSON.
    """
    lines = []

    # Group by priority
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    sorted_rules = sorted(
        rules,
        key=lambda x: priority_order.get(x.get("priority", "low"), 3),
    )

    current_priority = None
    for rule in sorted_rules:
        priority = rule.get("priority", "medium")

        # Print priority group header
        if priority != current_priority:
            current_priority = priority
            lines.append(f"\n── {priority.upper()} PRIORITY ──")

        lines.append(f"\nRULE {rule['rule_id']}: {rule['rule_name']}")

        if rule.get("enforce"):
            for e in rule["enforce"][:3]:   # max 4 enforce points per rule
                lines.append(f"  ✓ {e}")

        if rule.get("avoid"):
            for a in rule["avoid"][:1]:     # max 2 avoid points per rule
                lines.append(f"  ✗ {a}")

        if rule.get("reason"):
            lines.append(f"  → {rule['reason'][:80]}")  # truncate long reasons

    return "\n".join(lines)