# app/services/rule_service.py

from app.db.vector_store import (
    search_rules,
    get_rules_by_ids,
    get_collection_info,
)
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)


# ── Domain → Rule ID mapping ────────────────────────────────────
# When a domain is detected, these rules are ALWAYS included
# regardless of semantic search results
DOMAIN_MANDATORY_RULES = {
    "financial": [7, 27, 29, 51, 57, 103],
    "hr": [2, 4, 11, 18, 38, 69],
    "security_agency": [9, 35, 54, 94, 100],
    "real_estate": [25, 44, 60, 81, 83, 108],
    "e_learning": [4, 8, 74, 95, 104],
    "iot_wearables": [37, 52, 54, 80, 94],
    "multi_tenant_saas": [10, 34, 44, 96, 101, 107],
    "e_commerce": [3, 9, 27, 29, 38, 51],
    "land_acquisition": [4, 11, 12, 15, 16],
    "general": [1, 2, 3, 8, 9, 18, 30, 31, 32],
}

# Universal rules — always included for EVERY schema
UNIVERSAL_RULES = [1, 2, 3, 8, 9, 30]


def get_rules_for_requirement(
    requirement: str,
    domain: str = "general",
    top_k: int = None,
) -> dict:
    """
    Main function called by the engine.
    Returns a curated set of rules for a given requirement + domain.

    Steps:
    1. Semantic search on requirement text
    2. Add mandatory domain rules
    3. Add universal rules
    4. Deduplicate + sort by priority
    """
    k = top_k or settings.TOP_K_RULES

    # Step 1 — semantic search
    semantic_rules = search_rules(query=requirement, top_k=k)
    semantic_ids = [r["rule_id"] for r in semantic_rules]
    logger.info(f"🔍 Semantic search found {len(semantic_ids)} rules: {semantic_ids}")

    # Step 2 — mandatory domain rules
    domain_ids = DOMAIN_MANDATORY_RULES.get(domain.lower(), [])
    logger.info(f"📌 Domain '{domain}' mandatory rules: {domain_ids}")

    # Step 3 — universal rules always included
    logger.info(f"🌐 Universal rules: {UNIVERSAL_RULES}")

    # Step 4 — merge + deduplicate
    all_ids = list(dict.fromkeys(UNIVERSAL_RULES + domain_ids + semantic_ids))

    # Step 5 — fetch full rule objects for any IDs not in semantic results
    fetched_ids = set(semantic_ids)
    extra_ids = [i for i in all_ids if i not in fetched_ids]

    extra_rules = get_rules_by_ids(extra_ids) if extra_ids else []

    # Merge into one list
    all_rules = {r["rule_id"]: r for r in semantic_rules}
    for r in extra_rules:
        all_rules[r["rule_id"]] = r

    # Sort by priority order
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    sorted_rules = sorted(
        all_rules.values(),
        key=lambda x: priority_order.get(x.get("priority", "low"), 3),
    )

    logger.info(f"✅ Total rules assembled: {len(sorted_rules)}")

    return {
        "rules": sorted_rules,
        "total": len(sorted_rules),
        "semantic_matches": len(semantic_ids),
        "domain_mandatory": len(domain_ids),
        "universal": len(UNIVERSAL_RULES),
        "domain_used": domain,
    }


def get_rules_summary(rules: list[dict]) -> str:
    """
    Convert rule list into a compact summary string.
    Used for logging and debugging.
    """
    lines = []
    for r in rules:
        lines.append(
            f"  Rule {r['rule_id']:3d} [{r['priority']:8s}] {r['rule_name']}"
        )
    return "\n".join(lines)


def get_collection_stats() -> dict:
    """Return Qdrant collection stats."""
    return get_collection_info()