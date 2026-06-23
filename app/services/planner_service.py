# app/services/planner_service.py

import time
import logging
from app.engine.rule_matcher import match_rules
from app.prompts.system_prompt import (
    build_system_prompt,
    build_module_prompt,
    build_stitch_prompt,
)
from app.services.ai_service import generate_schema
from app.validators.schema_validator import SchemaValidator
from app.core.config import settings

logger = logging.getLogger(__name__)


def generate_database_schema(
    requirement: str,
    blueprint: dict = None,
    additional_context: str = None,
) -> dict:
    """
    Multi-pass schema generation.
    Generates one module at a time, stitches together.
    Target: 80-120 tables with full depth.
    """
    start_time = time.time()

    # ── Step 1: Match rules ──────────────────────────────────────
    match_result = match_rules(requirement)
    rules = match_result["rules"]
    primary_domain = match_result["primary_domain"]

    system_prompt = build_system_prompt(rules)

    # ── Step 2: Get modules from blueprint or requirement ────────
    if blueprint and blueprint.get("modules"):
        modules = blueprint["modules"]
        gst_required = blueprint.get("gst_required", False)
        scale = blueprint.get("scale", "medium")
        project_name = blueprint.get("project_name", "Project")
    else:
        # Generate blueprint on the fly
        from app.engine.architecture_planner import generate_deep_blueprint
        bp = generate_deep_blueprint(
            requirement=requirement,
            domain=primary_domain,
            gst_required="gst" in requirement.lower(),
            scale="medium",
        )
        modules = bp.get("modules", [])
        gst_required = bp.get("gst_required", False)
        scale = bp.get("scale", "medium")
        project_name = bp.get("project_name", "Project")

    logger.info(f"📋 Generating {len(modules)} modules...")

    # ── Step 3: Generate each module separately ──────────────────
    all_sql_parts = []
    generated_tables = []

    for i, module in enumerate(modules):
        logger.info(
            f"  Module {i+1}/{len(modules)}: {module['name']} "
            f"({len(module.get('tables', []))} tables)"
        )

        module_prompt = build_module_prompt(
            module=module,
            domain=primary_domain,
            gst_required=gst_required,
            scale=scale,
            existing_tables=generated_tables,
        )

        try:
            response = generate_schema(
                system_prompt=system_prompt,
                user_prompt=module_prompt,
            )
            module_sql = response["content"]

            # Track generated tables
            import re
            new_tables = re.findall(
                r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?',
                module_sql, re.IGNORECASE
            )
            generated_tables.extend(new_tables)

            all_sql_parts.append({
                "module": module["name"],
                "sql": module_sql,
                "tables": new_tables,
            })

            logger.info(
                f"  ✅ Module '{module['name']}': "
                f"{len(new_tables)} tables generated"
            )

        except Exception as e:
            logger.error(f"  ❌ Module '{module['name']}' failed: {e}")
            continue

    # ── Step 4: Stitch all modules ───────────────────────────────
    logger.info("🧵 Stitching modules together...")

    combined_sql = _stitch_modules(all_sql_parts, project_name)

    # ── Step 5: Validate combined schema ─────────────────────────
    validator = SchemaValidator()
    validation = validator.validate(combined_sql)
    total_tables = len(validation.tables_found)

    logger.info(
        f"📊 Combined schema: {total_tables} tables | "
        f"Score: {validation.score}/100"
    )

    # ── Step 6: Auto-fix if score < 80 ───────────────────────────
    if validation.score < 80 and validation.issues:
        logger.info("🔧 Running auto-fix pass...")
        combined_sql, validation = _run_fix_pass(
            combined_sql, validation, system_prompt
        )

    elapsed = round(time.time() - start_time, 2)

    return {
        "schema": combined_sql,
        "metadata": {
            "primary_domain": primary_domain,
            "all_domains": match_result["all_domains"],
            "domain_confidence": match_result["domain_confidence"],
            "rules_applied": [
                {
                    "rule_id": r["rule_id"],
                    "rule_name": r["rule_name"],
                    "priority": r["priority"],
                    "category": r["category"],
                }
                for r in rules
            ],
            "total_rules_applied": len(rules),
            "semantic_matches": match_result["semantic_matches"],
            "ai_provider": settings.AI_PROVIDER,
            "ai_model": settings.GROQ_MODEL,
            "token_usage": {"input_tokens": 0, "output_tokens": 0},
            "generation_time_seconds": elapsed,
            "modules_generated": len(all_sql_parts),
            "tables_per_module": [
                {"module": p["module"], "count": len(p["tables"])}
                for p in all_sql_parts
            ],
        },
        "validation": {
            "score": validation.score,
            "passed": validation.passed,
            "grade": (
                "A" if validation.score >= 90 else
                "B" if validation.score >= 80 else
                "C" if validation.score >= 70 else "D"
            ),
            "summary": validation.summary,
            "total_issues": validation.total_issues,
            "critical_issues": validation.critical_issues,
            "high_issues": validation.high_issues,
            "medium_issues": validation.medium_issues,
            "scores_breakdown": validation.scores_breakdown,
            "tables_found": validation.tables_found,
            "issues": [
                {
                    "rule_id": i.rule_id,
                    "rule_name": i.rule_name,
                    "severity": i.severity,
                    "issue": i.issue,
                    "suggestion": i.suggestion,
                    "table": i.table_name,
                }
                for i in validation.issues
            ],
        },
    }


def _stitch_modules(all_sql_parts: list[dict], project_name: str) -> str:
    """Combine all module SQL into one clean file."""
    from datetime import datetime

    header = f"""-- ============================================================
-- Project  : {project_name}
-- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
-- Modules  : {len(all_sql_parts)}
-- ============================================================

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

{_clean_sql(part['sql'])}
"""
        sections.append(section)

    footer = "\nCOMMIT;\n"
    return header + "\n".join(sections) + footer


def _clean_sql(sql: str) -> str:
    sql = sql.strip()
    if "```" in sql:
        parts = sql.split("```")
        sql = parts[1] if len(parts) > 1 else sql
        if sql.startswith("sql"):
            sql = sql[3:]
    return sql.strip()


def _run_fix_pass(
    sql: str,
    validation,
    system_prompt: str,
) -> tuple:
    """Single fix pass targeting specific issues."""
    if not validation.issues:
        return sql, validation

    critical_high = [
        i for i in validation.issues
        if i.severity in ["critical", "high"]
    ]

    if not critical_high:
        return sql, validation

    issue_text = "\n".join(
        f"- [{i.severity.upper()}] {i.issue} → {i.suggestion}"
        for i in critical_high[:10]
    )

    fix_prompt = f"""Fix ONLY these specific issues in the schema below.
Do NOT remove any tables. Do NOT simplify any columns.
Return the COMPLETE corrected schema.

ISSUES TO FIX:
{issue_text}

SCHEMA:
{sql[:6000]}"""

    try:
        response = generate_schema(
            system_prompt=system_prompt,
            user_prompt=fix_prompt,
        )
        fixed_sql = response["content"]

        from app.validators.schema_validator import SchemaValidator
        validator = SchemaValidator()
        new_validation = validator.validate(fixed_sql)

        if new_validation.score >= validation.score:
            logger.info(
                f"✅ Fix improved score: "
                f"{validation.score} → {new_validation.score}"
            )
            return fixed_sql, new_validation

    except Exception as e:
        logger.error(f"Fix pass failed: {e}")

    return sql, validation


def get_matched_rules_only(requirement: str) -> dict:
    match_result = match_rules(requirement)
    return {
        "primary_domain": match_result["primary_domain"],
        "all_domains": match_result["all_domains"],
        "domain_confidence": match_result["domain_confidence"],
        "total_rules": match_result["total_rules"],
        "semantic_matches": match_result["semantic_matches"],
        "rules": [
            {
                "rule_id": r["rule_id"],
                "rule_name": r["rule_name"],
                "priority": r["priority"],
                "category": r["category"],
                "trigger_when": r.get("trigger_when", [])[:2],
            }
            for r in match_result["rules"]
        ],
    }