# app/engine/rule_marketplace.py
"""
Rule Marketplace: Manages pluggable industry-specific rule packs
(Healthcare Pack, Financial Pack, Logistics Pack, etc.) that users can install.
"""

import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

RULE_PACKS = {
    "healthcare": {
        "name": "Healthcare Compliance Pack",
        "description": "Enforces HIPAA compliance, Patient Health Information (PHI) encryption flags, and patient consent audit logging.",
        "rules": [
            {
                "rule_id": 601,
                "rule_name": "HIPAA PHI Column Masking",
                "category": "security",
                "priority": "HIGH",
                "clause": "Any column storing patient name, date of birth, SSN, or medical record numbers must be marked with a comment indicating encryption or masking."
            },
            {
                "rule_id": 602,
                "rule_name": "Patient Consent Audit Trail",
                "category": "compliance",
                "priority": "CRITICAL",
                "clause": "All health record tables must be accompanied by a patient_consent_audit_log table tracking every view or update."
            }
        ]
    },
    "finance": {
        "name": "Financial Ledger Pack",
        "description": "Enforces double-entry bookkeeping constraints, PCI-DSS card security compliance, and high-precision currency decimals.",
        "rules": [
            {
                "rule_id": 701,
                "rule_name": "Double-Entry Ledger Integrity",
                "category": "integrity",
                "priority": "CRITICAL",
                "clause": "Any ledger transaction table must record debit and credit columns as separate rows or columns that must balance to zero."
            },
            {
                "rule_id": 702,
                "rule_name": "High-Precision Currency Columns",
                "category": "standards",
                "priority": "HIGH",
                "clause": "All monetary amount columns must use DECIMAL(18, 4) to prevent floating-point rounding errors."
            }
        ]
    },
    "logistics": {
        "name": "Logistics & Supply Chain Pack",
        "description": "Enforces spatial indexing for tracking, warehouse zoning foreign keys, and shipment state machine validation.",
        "rules": [
            {
                "rule_id": 801,
                "rule_name": "Geographic Coordinate Indexing",
                "category": "performance",
                "priority": "MEDIUM",
                "clause": "Tables storing latitude/longitude coordinates must have composite spatial indexes."
            },
            {
                "rule_id": 802,
                "rule_name": "Shipment State Lifecycle",
                "category": "workflow",
                "priority": "HIGH",
                "clause": "Shipment tables must include a status column restricted to: PLANNED, SHIPPED, IN_TRANSIT, DELIVERED, RETURNED."
            }
        ]
    }
}


def get_marketplace_packs() -> Dict[str, Dict[str, Any]]:
    """Returns all available rule packs."""
    return RULE_PACKS


def get_rules_from_installed_packs(installed_pack_ids: List[str]) -> List[dict]:
    """
    Returns the list of rule definitions for all installed packs.
    These are injected directly into the rule matcher.
    """
    active_rules = []
    for pack_id in installed_pack_ids:
        pack = RULE_PACKS.get(pack_id.lower())
        if pack:
            logger.info(f"📦 Loading rules from installed pack: {pack['name']}")
            active_rules.extend(pack["rules"])
    return active_rules
