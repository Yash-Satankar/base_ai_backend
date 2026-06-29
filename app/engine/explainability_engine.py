# app/engine/explainability_engine.py
"""
The Explainability Engine: Generates a structured traceability graph
explaining the business and architectural rationale behind every database object.
"""

import logging
from typing import Dict, Any
from app.services.ai_service import generate_schema
from app.engine.abstraction_pipeline import _clean_and_parse_json

logger = logging.getLogger(__name__)


def generate_traceability_graph(
    sql: str,
    blueprint_json: dict,
    l1_json: dict,
    l2_json: dict,
    l3_json: dict,
    l4_json: dict,
    rules_applied: list,
) -> dict:
    """
    Generates a mapping of every table in the SQL schema back to its business
    and technical origins.
    """
    system_prompt = """You are an Enterprise Database Auditor.
Given the compiled SQL schema, the intermediate architectural specifications (L1-L4), and the rules applied:
Build a structured Traceability Graph explaining the exact rationale behind every table.

For EVERY table in the SQL schema, provide:
1. 'originating_capability': The Level 2 Capability that required it.
2. 'originating_workflows': List of Level 3 Workflows utilizing it.
3. 'rules_triggered': List of Rule IDs that influenced this table's design.
4. 'reusable_component': The name of the reusable engine (e.g. LedgerEngine) if applicable.
5. 'design_rationale': Why was this table structured this way? (E.g. explain why it has an archive table, a lifecycle table, or specific columns).
6. 'alternatives_considered': What simpler/different design was rejected and why?

Return ONLY valid JSON matching this schema:
{
  "tables": {
    "table_name_all": {
      "originating_capability": "Billing & Payments",
      "originating_workflows": ["Process Refund"],
      "rules_triggered": [7, 27],
      "reusable_component": "LedgerEngine",
      "design_rationale": "Requires double-entry debit/credit ledger structure with decimal(15,4) precision to prevent rounding errors.",
      "alternatives_considered": "Single column balance updating (Rejected because it lacks an audit trail and violates GAAP compliance)."
    }
  }
}"""

    user_prompt = f"""SQL Schema:
{sql[:4000]}

L1 Understanding:
{json_dumps(l1_json)}

L2 Capabilities:
{json_dumps(l2_json)}

L3 Workflows:
{json_dumps(l3_json)}

L4 Entities:
{json_dumps(l4_json)}

Rules Applied:
{json_dumps(rules_applied[:15])}"""

    try:
        response = generate_schema(
            system_prompt=system_prompt,
            user_prompt=user_prompt
        )
        graph = _clean_and_parse_json(response["content"])
        logger.info("✅ Traceability graph successfully generated")
        return graph
    except Exception as e:
        logger.error(f"❌ Failed to generate traceability graph: {e}", exc_info=True)
        return {
            "error": f"Traceability generation failed: {str(e)}",
            "tables": {}
        }


def json_dumps(data: Any) -> str:
    import json
    return json.dumps(data, indent=2)
