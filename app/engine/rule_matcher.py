# app/engine/rule_matcher.py

import re
from app.services.rule_service import get_rules_for_requirement
import logging

logger = logging.getLogger(__name__)


# ── Keyword → Domain classifier ─────────────────────────────────
DOMAIN_KEYWORDS = {
    "financial": [
        "wallet", "payment", "invoice", "billing", "transaction",
        "ledger", "balance", "gst", "tax", "salary", "payroll",
        "reimbursement", "claim", "loan", "emi", "interest",
        "credit", "debit", "refund", "settlement", "commission",
    ],
    "hr": [
        "employee", "staff", "attendance", "leave", "payroll",
        "department", "designation", "appraisal", "recruitment",
        "onboarding", "offboarding", "shift", "overtime", "hr",
    ],
    "security_agency": [
        "guard", "security", "patrol", "campus", "shift",
        "agency", "supervisor", "duty", "roster", "uniform",
        "visitor", "verification", "badge",
    ],
    "real_estate": [
        "property", "broker", "flat", "plot", "house", "rent",
        "sale", "lease", "tenant", "landlord", "listing",
        "real estate", "realty", "apartment", "commercial",
    ],
    "e_learning": [
    "student", "course", "quiz", "exam", "question",
    "assignment", "teacher", "lecture", "grade", "score",
    "certificate", "enrollment", "learning", "education",
    "competition", "slot", "participant",
    "school", "college", "fee", "class", "semester",  # ← add these
    "admission", "academic", "result", "marks",        # ← add these
],
    "iot_wearables": [
        "device", "sensor", "iot", "wearable", "watch",
        "tracker", "gps", "battery", "firmware", "alert",
        "heartbeat", "location", "realtime",
    ],
    "multi_tenant_saas": [
        "tenant", "subscription", "plan", "license", "saas",
        "organisation", "workspace", "multi-tenant", "onboarding",
        "feature flag", "module", "permission",
    ],
    "e_commerce": [
        "product", "cart", "order", "shipping", "inventory",
        "stock", "catalogue", "sku", "warehouse", "delivery",
        "return", "refund", "marketplace",
    ],
    "land_acquisition": [
        "land", "survey", "acquisition", "village", "taluka",
        "district", "plot", "khasra", "registration", "document",
        "ownership", "title",
    ],
}


def detect_domain(requirement: str) -> tuple[str, float]:
    """
    Detect the primary domain from user requirement text.
    Returns (domain_name, confidence_score).
    Confidence = matched_keywords / total_domain_keywords
    """
    requirement_lower = requirement.lower()

    scores = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        matched = sum(
            1 for kw in keywords
            if re.search(r'\b' + re.escape(kw) + r'\b', requirement_lower)
        )
        if matched > 0:
            scores[domain] = matched

    if not scores:
        logger.info("🌐 No domain detected — using 'general'")
        return "general", 0.0

    best_domain = max(scores, key=scores.get)
    confidence = round(scores[best_domain] / len(DOMAIN_KEYWORDS[best_domain]), 2)

    logger.info(f"🎯 Domain detected: '{best_domain}' (confidence: {confidence})")
    logger.info(f"   All scores: {scores}")

    return best_domain, confidence


def detect_all_domains(requirement: str) -> list[str]:
    """
    Detect ALL matching domains (requirement can span multiple).
    e.g. 'HR system with payroll and wallet' → ['hr', 'financial']
    """
    requirement_lower = requirement.lower()
    matched_domains = []

    for domain, keywords in DOMAIN_KEYWORDS.items():
        matched = sum(
            1 for kw in keywords
            if re.search(r'\b' + re.escape(kw) + r'\b', requirement_lower)
        )
        if matched >= 2:   # at least 2 keyword hits = domain is relevant
            matched_domains.append(domain)

    if not matched_domains:
        matched_domains = ["general"]

    logger.info(f"🎯 All matched domains: {matched_domains}")
    return matched_domains


def match_rules(requirement: str) -> dict:
    """
    Full rule matching pipeline:
    1. Detect domain(s)
    2. Fetch rules per domain
    3. Merge if multi-domain
    4. Return final rule set ready for prompt injection
    """
    # Detect primary domain
    primary_domain, confidence = detect_domain(requirement)

    # Detect all relevant domains
    all_domains = detect_all_domains(requirement)

    # Get rules — use primary domain for mandatory rules
    rule_result = get_rules_for_requirement(
        requirement=requirement,
        domain=primary_domain,
    )

    # If multi-domain, add mandatory rules for secondary domains too
    if len(all_domains) > 1:
        from app.services.rule_service import (
            DOMAIN_MANDATORY_RULES,
            get_rules_by_ids,
        )

        extra_ids = []
        for domain in all_domains:
            if domain != primary_domain:
                extra_ids.extend(DOMAIN_MANDATORY_RULES.get(domain, []))

        if extra_ids:
            # Deduplicate against already fetched rules
            existing_ids = {r["rule_id"] for r in rule_result["rules"]}
            new_ids = [i for i in extra_ids if i not in existing_ids]
            if new_ids:
                extra_rules = get_rules_by_ids(new_ids)
                rule_result["rules"].extend(extra_rules)
                rule_result["total"] = len(rule_result["rules"])
                logger.info(f"➕ Added {len(new_ids)} extra rules for secondary domains")

    return {
        "rules": rule_result["rules"],
        "total_rules": rule_result["total"],
        "primary_domain": primary_domain,
        "all_domains": all_domains,
        "domain_confidence": confidence,
        "semantic_matches": rule_result["semantic_matches"],
    }