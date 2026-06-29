# app/engine/score_engine.py
"""
Architecture Score Engine: Deterministically evaluates a database blueprint
across critical enterprise design patterns. Zero LLM overhead.
"""

import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


def evaluate_blueprint(blueprint: dict) -> dict:
    """
    Deterministically scores a logical blueprint JSON out of 100.
    Returns a breakdown of scores and findings.
    """
    logger.info("📐 Running deterministic score evaluation on blueprint...")

    # Extract all tables from all modules
    tables = []
    modules = blueprint.get("modules", [])
    for module in modules:
        tables.extend(module.get("tables", []))

    if not tables:
        return {
            "overall_score": 0.0,
            "normalization_score": 0.0,
            "audit_score": 0.0,
            "lifecycle_score": 0.0,
            "index_score": 0.0,
            "financial_score": 0.0,
            "approval_score": 0.0,
            "findings": ["No tables found in blueprint."]
        }

    findings: List[str] = []
    table_names = {t["name"] for t in tables}

    # ── 1. Normalization (15 points) ──
    normalization_score = 15.0
    bad_pk_count = 0
    bad_fk_count = 0
    for t in tables:
        cols = {c["name"] for c in t.get("columns", [])}
        # Check PK naming
        if "id" not in cols:
            bad_pk_count += 1
        # Check FK naming (any column ending in id but not _id or id)
        for col in cols:
            if col.endswith("id") and not col.endswith("_id") and col != "id":
                bad_fk_count += 1

    if bad_pk_count > 0:
        deduction = min(10.0, bad_pk_count * 2.0)
        normalization_score -= deduction
        findings.append(f"Normalization: {bad_pk_count} table(s) lack a standard 'id' primary key.")
    if bad_fk_count > 0:
        deduction = min(5.0, bad_fk_count * 1.0)
        normalization_score -= deduction
        findings.append(f"Normalization: {bad_fk_count} column(s) violate '_id' foreign key naming convention.")

    # ── 2. Audit Strategy (15 points) ──
    audit_score = 15.0
    headers_without_archives = 0
    for t_name in table_names:
        if t_name.endswith("_header_all"):
            archive_name = t_name.replace("_header_all", "_archive_all")
            if archive_name not in table_names:
                headers_without_archives += 1

    if headers_without_archives > 0:
        deduction = min(15.0, headers_without_archives * 3.0)
        audit_score -= deduction
        findings.append(f"Audit: {headers_without_archives} master header table(s) lack an '_archive_all' mirror.")

    # ── 3. Lifecycle Design (15 points) ──
    lifecycle_score = 15.0
    headers_without_lifecycles = 0
    for t in tables:
        t_name = t["name"]
        cols = {c["name"] for c in t.get("columns", [])}
        if t_name.endswith("_header_all") and "status" in cols:
            lc_name = t_name.replace("_header_all", "_life_cycle_all")
            if lc_name not in table_names:
                headers_without_lifecycles += 1

    if headers_without_lifecycles > 0:
        deduction = min(15.0, headers_without_lifecycles * 3.0)
        lifecycle_score -= deduction
        findings.append(f"Lifecycle: {headers_without_lifecycles} state-changing table(s) lack a status '_life_cycle_all' log.")

    # ── 4. Index Strategy (20 points) ──
    index_score = 20.0
    missing_index_count = 0
    for t in tables:
        for col in t.get("columns", []):
            c_name = col["name"]
            # If it's a foreign key, check if there's an index or if the column comment indicates indexing
            if c_name.endswith("_id") and c_name != "id":
                comment = col.get("comment", "").lower()
                if "index" not in comment and "key" not in comment:
                    missing_index_count += 1

    if missing_index_count > 0:
        deduction = min(20.0, missing_index_count * 2.0)
        index_score -= deduction
        findings.append(f"Indexing: {missing_index_count} foreign key column(s) lack explicit index declarations in comments.")

    # ── 5. Financial Design (15 points) ──
    financial_score = 15.0
    improper_decimal_count = 0
    missing_balance_count = 0
    for t in tables:
        t_name = t["name"]
        for col in t.get("columns", []):
            c_name = col["name"].lower()
            c_type = col["type"].lower()
            if any(w in c_name for w in ["amount", "price", "balance", "cost", "salary"]):
                if "decimal" not in c_type:
                    improper_decimal_count += 1
        if "transaction" in t_name:
            cols = {c["name"].lower() for c in t.get("columns", [])}
            if "closing_balance" not in cols and "balance" not in cols:
                missing_balance_count += 1

    if improper_decimal_count > 0:
        deduction = min(7.5, improper_decimal_count * 2.5)
        financial_score -= deduction
        findings.append(f"Financial: {improper_decimal_count} monetary column(s) use FLOAT/DOUBLE instead of DECIMAL.")
    if missing_balance_count > 0:
        deduction = min(7.5, missing_balance_count * 3.5)
        financial_score -= deduction
        findings.append(f"Financial: {missing_balance_count} transaction table(s) lack a 'closing_balance' column.")

    # ── 6. Approval Workflows & RBAC (20 points) ──
    approval_score = 20.0
    has_approvals = any("approval" in t_name for t_name in table_names)
    has_rbac = any(any(w in t_name for w in ["role", "permission", "rbac"]) for t_name in table_names)

    if not has_approvals:
        approval_score -= 10.0
        findings.append("Approvals: Missing dedicated workflow approval tables.")
    if not has_rbac:
        approval_score -= 10.0
        findings.append("RBAC: Missing role-based access control or permission tables.")

    # Calculate overall score
    overall_score = round(
        normalization_score + audit_score + lifecycle_score + index_score + financial_score + approval_score,
        1
    )

    return {
        "overall_score": max(0.0, overall_score),
        "normalization_score": normalization_score,
        "audit_score": audit_score,
        "lifecycle_score": lifecycle_score,
        "index_score": index_score,
        "financial_score": financial_score,
        "approval_score": approval_score,
        "findings": findings
    }
