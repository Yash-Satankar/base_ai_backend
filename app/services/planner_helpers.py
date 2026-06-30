# app/services/planner_helpers.py
"""
Planner Helpers: Encapsulates SQL stitching, cleaning, and targeted fix passes
to keep the main planner service clean and modular.
"""

import re
import logging
from datetime import datetime
from app.services.ai_service import generate_schema

logger = logging.getLogger(__name__)


def batch_tables(tables: list[dict], batch_size: int) -> list[list[dict]]:
    """
    Split a module's table list into batches of `batch_size`.
    Keeps each AI call well within the model's output token limit.
    """
    return [
        tables[i: i + batch_size]
        for i in range(0, len(tables), batch_size)
    ]


def stitch_modules(all_sql_parts: list[dict], project_name: str) -> str:
    """Combine all module SQL into one clean, importable file."""
    total_tables = sum(len(p["tables"]) for p in all_sql_parts)

    header = f"""-- ============================================================
-- Project  : {project_name}
-- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
-- Modules  : {len(all_sql_parts)}
-- Tables   : {total_tables}
-- ============================================================

SET FOREIGN_KEY_CHECKS = 0;
SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
SET time_zone = "+00:00";
START TRANSACTION;

"""
    sections = []
    for part in all_sql_parts:
        section = f"""
-- ============================================================
-- MODULE: {part['module']}  ({len(part['tables'])} tables)
-- ============================================================

{clean_sql(part['sql'])}
"""
        sections.append(section)

    footer = "\nCOMMIT;\nSET FOREIGN_KEY_CHECKS = 1;\n"
    return header + "\n".join(sections) + footer


def clean_sql(sql: str) -> str:
    """Remove markdown code block fences."""
    sql = sql.strip()
    if "```" in sql:
        parts = sql.split("```")
        sql = parts[1] if len(parts) > 1 else sql
        if sql.startswith("sql"):
            sql = sql[3:]
    return sql.strip()


def run_fix_pass(
    sql: str,
    validation,
    system_prompt: str,
) -> tuple:
    """
    Targeted fix pass: extracts only the problematic table blocks
    rather than truncating the entire schema.  This works correctly
    for large schemas that would otherwise exceed token limits.
    """
    if not validation.issues:
        return sql, validation

    critical_high = [
        i for i in validation.issues
        if i.severity in ["critical", "high"]
    ]
    if not critical_high:
        return sql, validation

    # Gather the specific table names that have issues
    problem_tables = list({
        i.table_name for i in critical_high if i.table_name
    })[:6]  # cap at 6 tables per fix pass to stay within token limits

    issue_text = "\n".join(
        f"- [{i.severity.upper()}] {i.issue} → {i.suggestion}"
        for i in critical_high[:12]
    )

    # Extract only the relevant table blocks from the full SQL
    targeted_sql = extract_table_blocks(sql, problem_tables) if problem_tables else sql[:8000]

    fix_prompt = f"""Fix ONLY these specific issues in the table blocks below.
Do NOT remove any tables. Do NOT simplify any columns.
Return ONLY the corrected CREATE TABLE blocks — no extra commentary.

ISSUES TO FIX:
{issue_text}

TABLE BLOCKS TO FIX:
{targeted_sql}"""

    try:
        response = generate_schema(
            system_prompt=system_prompt,
            user_prompt=fix_prompt,
        )
        fixed_blocks = clean_sql(response["content"])

        # Splice fixed blocks back into the full schema
        fixed_sql = splice_fixed_blocks(sql, fixed_blocks, problem_tables)

        from app.validators.schema_validator import SchemaValidator
        validator = SchemaValidator()
        new_validation = validator.validate(fixed_sql)

        if new_validation.score >= validation.score:
            logger.info(
                f"✅ Fix pass improved score: "
                f"{validation.score} → {new_validation.score}"
            )
            return fixed_sql, new_validation

    except Exception as e:
        logger.error(f"Fix pass failed: {e}")

    return sql, validation


def extract_table_blocks(sql: str, table_names: list[str]) -> str:
    """
    Extract specific CREATE TABLE blocks from a large SQL string.
    Returns only the blocks for the requested table names.
    """
    blocks = []
    for name in table_names:
        pattern = re.compile(
            rf'(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?{re.escape(name)}`?\s*\(.*?\))\s*(?:ENGINE[^;]*)?;',
            re.IGNORECASE | re.DOTALL
        )
        match = pattern.search(sql)
        if match:
            blocks.append(match.group(0))
    return "\n\n".join(blocks) if blocks else sql[:8000]


def splice_fixed_blocks(original_sql: str, fixed_blocks: str, table_names: list[str]) -> str:
    """
    Replace specific table blocks in the full SQL with the fixed versions.
    If extraction/re-insertion fails, returns the original.
    """
    result = original_sql
    for name in table_names:
        fixed_pattern = re.compile(
            rf'(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?{re.escape(name)}`?\s*\(.*?\))\s*(?:ENGINE[^;]*)?;',
            re.IGNORECASE | re.DOTALL
        )
        fixed_match = fixed_pattern.search(fixed_blocks)
        orig_match  = fixed_pattern.search(result)
        if fixed_match and orig_match:
            result = result[:orig_match.start()] + fixed_match.group(0) + result[orig_match.end():]
    return result
