# app/services/planner_service.py

from app.engine.rule_matcher import match_rules
from app.prompts.system_prompt import build_system_prompt, build_user_prompt
from app.services.ai_service import generate_schema
import logging
import time
from app.validators.schema_validator import SchemaValidator
from app.validators.sql_syntax_validator import validate_sql_syntax

logger = logging.getLogger(__name__)


def generate_database_schema(
    requirement: str,
    additional_context: str = None,
) -> dict:
    """
    Main pipeline — orchestrates everything:
    1. Detect domain + match rules
    2. Build system prompt with injected rules
    3. Build user prompt
    4. Call AI
    5. Return structured response
    """
    start_time = time.time()
    logger.info(f"🚀 Starting schema generation for: {requirement[:80]}...")

    # ── Step 1: Match rules ──────────────────────────────────────
    logger.info("Step 1/4: Matching rules...")
    match_result = match_rules(requirement)

    rules = match_result["rules"]
    primary_domain = match_result["primary_domain"]
    all_domains = match_result["all_domains"]
    total_rules = match_result["total_rules"]

    logger.info(f"  Domain: {primary_domain} | Rules matched: {total_rules}")

    # ── Step 2: Build prompts ────────────────────────────────────
    logger.info("Step 2/4: Building prompts...")
    system_prompt = build_system_prompt(rules)
    user_prompt = build_user_prompt(
        requirement=requirement,
        domain=primary_domain,
        additional_context=additional_context,
    )

    logger.info(f"  System prompt: {len(system_prompt)} chars")
    logger.info(f"  User prompt:   {len(user_prompt)} chars")

    # ── Step 3: Generate schema ──────────────────────────────────
    logger.info("Step 3/4: Calling AI...")
    ai_response = generate_schema(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    # ── Step 4: Package response ─────────────────────────────────
    logger.info("Step 4/4: Packaging response...")
    elapsed = round(time.time() - start_time, 2)

    response = {
        "schema": ai_response["content"],
        "metadata": {
            "primary_domain": primary_domain,
            "all_domains": all_domains,
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
            "total_rules_applied": total_rules,
            "semantic_matches": match_result["semantic_matches"],
            "ai_provider": ai_response["provider"],
            "ai_model": ai_response["model"],
            "token_usage": ai_response["usage"],
            "generation_time_seconds": elapsed,
        },
    }
    # app/services/planner_service.py

    # ── Step 5: Validate schema ──────────────────────────────────
    logger.info("Step 5/4: Validating generated schema...")
    validator = SchemaValidator()
    validation = validator.validate(ai_response["content"])

    response["validation"] = {
        "score":            validation.score,
        "passed":           validation.passed,
        "grade":            (
            "A" if validation.score >= 90 else
            "B" if validation.score >= 80 else
            "C" if validation.score >= 70 else
            "D" if validation.score >= 60 else "F"
        ),
        "summary":          validation.summary,
        "total_issues":     validation.total_issues,
        "critical_issues":  validation.critical_issues,
        "high_issues":      validation.high_issues,
        "medium_issues":    validation.medium_issues,
        "scores_breakdown": validation.scores_breakdown,
        "tables_found":     validation.tables_found,
        "issues": [
            {
                "rule_id":    i.rule_id,
                "rule_name":  i.rule_name,
                "severity":   i.severity,
                "issue":      i.issue,
                "suggestion": i.suggestion,
                "table":      i.table_name,
            }
            for i in validation.issues
        ],
    }
    # ── SQL syntax check ─────────────────────────────────────────
    syntax_check = validate_sql_syntax(ai_response["content"])
    if not syntax_check["valid"]:
        logger.warning(
            f"SQL syntax issues: {syntax_check['issues']}"  
        )
    response["syntax_check"] = syntax_check

    logger.info(f"📊 Validation: {validation.summary}")
    logger.info(f"📊 Syntax Check: {syntax_check['issues']}")
    return response


def get_matched_rules_only(requirement: str) -> dict:
    """
    Dry run — returns matched rules WITHOUT generating schema.
    Useful for debugging and showing users which rules will apply.
    """
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