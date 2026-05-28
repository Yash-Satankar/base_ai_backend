# app/engine/rule_matcher.py

import re
from app.services.rule_service import get_rules_for_requirement
import logging

logger = logging.getLogger(__name__)


DOMAIN_KEYWORDS = {
    "financial": [
        "wallet", "payment", "invoice", "billing", "transaction",
        "ledger", "balance", "gst", "tax", "salary", "payroll",
        "reimbursement", "claim", "loan", "emi", "interest",
        "credit", "debit", "refund", "settlement", "commission",
        "revenue", "expense", "budget", "receipt", "fund",
    ],
    "hr": [
        "employee", "staff", "attendance", "leave", "payroll",
        "department", "designation", "appraisal", "recruitment",
        "onboarding", "offboarding", "shift", "overtime", "hr",
        "human resource", "workforce", "hiring", "resignation",
    ],
    "security_agency": [
        "guard", "security agency", "patrol", "campus", "duty shift",
        "guard agency", "guard supervisor", "guard duty",
        "visitor verification", "visitor management",
        "security badge", "security roster",
    ],
    "real_estate": [
        "property", "broker", "flat", "plot", "house", "rent",
        "sale", "lease", "tenant", "landlord", "listing",
        "real estate", "realty", "apartment", "commercial property",
        "property owner", "property buyer",
    ],
    "e_learning": [
        "student", "course", "quiz", "exam", "question paper",
        "assignment", "teacher", "lecture", "grade", "marks",
        "certificate", "enrollment", "learning management",
        "education", "school management", "college management",
        "fee collection", "class management", "semester",
        "admission", "academic", "result", "classroom",
        "study material", "homework",
    ],
    "iot_wearables": [
        "device", "sensor", "iot", "wearable", "smartwatch",
        "tracker", "gps tracking", "battery status", "firmware",
        "heartbeat", "realtime location", "device management",
        "hardware", "embedded",
    ],
    "multi_tenant_saas": [
        "tenant", "subscription", "saas", "licence",
        "organisation settings", "multi-tenant", "workspace",
        "feature flag", "plan management", "billing portal",
        "white label",
    ],
    "e_commerce": [
        "product catalogue", "cart", "order management", "shipping",
        "inventory", "stock", "sku", "warehouse", "delivery",
        "return policy", "marketplace", "storefront",
        "product listing", "checkout",
    ],
    "land_acquisition": [
        "land", "survey number", "land acquisition", "village",
        "taluka", "khasra", "registration document",
        "ownership", "title deed", "land record",
    ],

    # ── NEW DOMAINS ───────────────────────────────────────────────

    "corporate_enterprise": [
        "meeting", "corporate", "investor", "broker",
        "analyst", "scheduling", "calendar", "appointment",
        "conference", "boardroom", "agenda", "minutes",
        "plant visit", "site visit", "roadshow",
        "investor relations", "corporate communication",
        "engagement platform", "one-on-one", "group meeting",
        "meeting workflow", "meeting management",
        "executive assistant", "coordinator",
        "google meet", "microsoft teams", "zoom",
        "participant management", "meeting platform",
        "online meeting", "offline meeting",
    ],
    "healthcare": [
        "patient", "doctor", "hospital", "clinic", "diagnosis",
        "prescription", "appointment booking", "medical record",
        "pharmacy", "nurse", "ward", "opd", "ipd",
        "lab report", "health record", "treatment",
    ],
    "logistics": [
        "shipment", "delivery", "fleet", "driver", "vehicle",
        "route", "logistics", "warehouse management",
        "dispatch", "tracking", "freight", "cargo",
        "last mile", "courier",
    ],
    "project_management": [
        "project", "task", "milestone", "sprint", "kanban",
        "project management", "team collaboration",
        "deadline", "backlog", "workflow automation",
        "gantt", "resource allocation",
    ],

    "general": [
        "database", "system", "application", "platform",
        "management system", "web application",
    ],
}

# app/engine/rule_matcher.py
# Add this function:

def detect_domain_with_ai(requirement: str) -> tuple[str, float]:
    """
    Use AI to detect domain when keyword matching gives low confidence.
    Falls back to keyword detection result if AI fails.
    """
    from app.services.ai_service import generate_schema

    domains_list = ", ".join(DOMAIN_KEYWORDS.keys())

    system_prompt = f"""You are a domain classifier for database requirements.
Classify the requirement into exactly ONE of these domains:
{domains_list}

Return ONLY the domain name — nothing else.
No explanation, no punctuation, just the domain name exactly as listed."""

    user_prompt = f"Requirement: {requirement[:500]}"

    try:
        response = generate_schema(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        detected = response["content"].strip().lower().replace(" ", "_")

        # Validate it's a known domain
        if detected in DOMAIN_KEYWORDS:
            logger.info(f"🤖 AI domain detection: '{detected}'")
            return detected, 0.85
        else:
            logger.warning(f"AI returned unknown domain: '{detected}'")
            return "general", 0.3

    except Exception as e:
        logger.error(f"AI domain detection failed: {e}")
        return "general", 0.3

def detect_domain(requirement: str) -> tuple[str, float]:
    """
    Detect primary domain.
    Uses keyword matching first.
    Falls back to AI detection when confidence is low.
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
        logger.info("No keyword matches — using AI domain detection")
        return detect_domain_with_ai(requirement)

    best_domain = max(scores, key=scores.get)
    confidence = round(
        scores[best_domain] / len(DOMAIN_KEYWORDS[best_domain]), 2
    )

    logger.info(f"🎯 Keyword domain: '{best_domain}' (confidence: {confidence})")
    logger.info(f"   All scores: {scores}")

    # ── Use AI if confidence is too low ──────────────────────────
    CONFIDENCE_THRESHOLD = 0.15

    if confidence < CONFIDENCE_THRESHOLD:
        logger.info(
            f"Confidence {confidence} < {CONFIDENCE_THRESHOLD} "
            f"— switching to AI detection"
        )
        ai_domain, ai_confidence = detect_domain_with_ai(requirement)

        # Use AI result only if it's more confident
        if ai_confidence > confidence:
            return ai_domain, ai_confidence

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