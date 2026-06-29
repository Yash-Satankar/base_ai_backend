# app/engine/recommendation_engine.py
"""
Recommendation Engine: Analyzes the current database design and queries the
Knowledge Graph to suggest missing entities, tables, or modules.
"""

import logging
from typing import List, Dict, Any
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import GraphNode, GraphEdge

logger = logging.getLogger(__name__)


# High-value fallback enterprise association rules
FALLBACK_ASSOCIATIONS = [
    {
        "trigger_keywords": ["inventory", "stock", "warehouse"],
        "missing_keywords": ["reservation", "reserve"],
        "recommendation": {
            "title": "Stock Reservation Module",
            "description": "Most enterprise inventory systems include a Stock Reservation table to lock stock during checkout before physical dispatch.",
            "suggested_table": "stock_reservation_transaction_all"
        }
    },
    {
        "trigger_keywords": ["payment", "invoice", "billing"],
        "missing_keywords": ["refund", "settlement"],
        "recommendation": {
            "title": "Refund & Settlement Ledger",
            "description": "Financial billing systems require a dedicated Refund Ledger to trace partial refunds and vendor settlements against original invoices.",
            "suggested_table": "refund_transaction_ledger_all"
        }
    },
    {
        "trigger_keywords": ["user", "account", "customer"],
        "missing_keywords": ["session", "device"],
        "recommendation": {
            "title": "Device & Session Audit Trail",
            "description": "To comply with security standards, customer accounts should track active sessions and device fingerprints to prevent session hijacking.",
            "suggested_table": "user_session_audit_log_all"
        }
    }
]


async def generate_recommendations(blueprint_json: dict, db: AsyncSession) -> List[dict]:
    """
    Generates proactive architectural recommendations by querying the
    Knowledge Graph and matching against fallback association rules.
    """
    logger.info("🔍 Generating proactive architecture recommendations...")
    recommendations = []

    modules = blueprint_json.get("modules", [])
    table_names = [t["name"].lower() for m in modules for t in m.get("tables", [])]

    # 1. Query the Knowledge Graph for co-occurring nodes
    # We look for nodes commonly linked to our existing table names in other projects
    try:
        for table in table_names:
            # Query edges where source is this table and target is another table
            # (In a fully populated graph, this would return highly weighted co-occurrences)
            stmt = (
                select(GraphNode.name, GraphEdge.properties)
                .join(GraphEdge, GraphEdge.target_id == GraphNode.id)
                .join(GraphNode, GraphNode.id == GraphEdge.source_id)
                .where(GraphNode.name == table, GraphEdge.edge_type == "requires")
            )
            result = await db.execute(stmt)
            for row in result.all():
                target_name = row[0]
                if target_name not in table_names:
                    recommendations.append({
                        "title": f"Suggested Entity: {target_name}",
                        "description": f"Frequently generated alongside '{table}' in similar projects.",
                        "suggested_table": target_name
                    })
    except Exception as e:
        logger.warning(f"Failed to query recommendations from Knowledge Graph: {e}")

    # 2. Match against fallback association rules
    for assoc in FALLBACK_ASSOCIATIONS:
        # Check if any trigger keyword is present in the current tables
        has_trigger = any(any(kw in t for kw in assoc["trigger_keywords"]) for t in table_names)
        # Check if the missing keywords are absent
        is_missing = not any(any(kw in t for kw in assoc["missing_keywords"]) for t in table_names)

        if has_trigger and is_missing:
            recommendations.append(assoc["recommendation"])

    return recommendations
