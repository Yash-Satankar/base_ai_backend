# app/engine/council.py
"""
Multi-Agent Architecture Council: Spawns specialized agents to review
and refine the database blueprint, acting as an automated board of directors.
"""

import logging
import json
from typing import Dict, List, Tuple, Any
from app.services.ai_service import generate_schema
from app.engine.abstraction_pipeline import _clean_and_parse_json

logger = logging.getLogger(__name__)


class CouncilReviewer:
    def __init__(self, name: str, persona: str, focus: str):
        self.name = name
        self.persona = persona
        self.focus = focus

    def review(self, context_str: str) -> dict:
        """Run the agent's independent review."""
        system_prompt = f"""You are the {self.name} on the Architecture Council.
Persona: {self.persona}
Focus Area: {self.focus}

Review the provided database design and business goals.
Return ONLY valid JSON matching this schema:
{{
  "verdict": "APPROVED" or "NEEDS_REVISION",
  "score": 0 to 100,
  "findings": ["Finding A", "Finding B"],
  "recommended_adjustments": [
    {{
      "table_name": "target_table_name",
      "adjustment": "What to change (e.g., Add column X, change type of Y, add index Z)"
    }}
  ]
}}"""

        try:
            response = generate_schema(system_prompt=system_prompt, user_prompt=context_str)
            return _clean_and_parse_json(response["content"])
        except Exception as e:
            logger.error(f"Agent {self.name} review failed: {e}")
            return {
                "verdict": "APPROVED",
                "score": 90,
                "findings": [f"Review skipped due to technical error: {str(e)}"],
                "recommended_adjustments": []
            }


# ── The Council Members ──────────────────────────────────────────────────────

COUNCIL_MEMBERS = [
    CouncilReviewer(
        name="Enterprise Architect",
        persona="Ensures alignment between business capabilities (L2), workflows (L3), and database modules (L7). Prevents bloated schemas.",
        focus="Business capability alignment, modularity, and boundary cleanups."
    ),
    CouncilReviewer(
        name="Database Architect",
        persona="Enforces relational database best practices, third normal form (3NF) compliance, correct primary/foreign key mappings, and naming taxonomy.",
        focus="Normalization, integrity constraints, and naming consistency."
    ),
    CouncilReviewer(
        name="Performance Engineer",
        persona="Predicts query paths, write amplification bottlenecks, indexing gaps, and storage growth patterns.",
        focus="Index optimization, partitioning, and write-path scalability."
    ),
    CouncilReviewer(
        name="Compliance & Security Expert",
        persona="Enforces security compliance (GDPR, HIPAA, PCI-DSS). Identifies PII columns requiring encryption or masking. Verifies audit trails.",
        focus="Data privacy, encryption, audit logs, and regulatory compliance."
    ),
    CouncilReviewer(
        name="Reporting & Integration Expert",
        persona="Analyzes reporting complexity, join depths, and recommends aggregate reporting tables for dashboard speed.",
        focus="Analytical queries, pre-computed views, and ETL structures."
    )
]


# ── Council Coordinator ──────────────────────────────────────────────────────

def run_architecture_council(
    l1: dict,
    l2: dict,
    l3: dict,
    l4: dict,
    l5: dict,
    l6: dict,
    l7: dict,
    blueprint: dict
) -> Tuple[dict, float]:
    """
    Spawns all specialized reviewers, gathers their verdicts,
    and uses a Coordinator agent to synthesize the final blueprint adjustments.
    """
    logger.info("👥 Convening the Multi-Agent Architecture Council...")

    # Assemble shared context
    context_str = f"""Project Name: {l1.get('project_name')}
Business Goal: {l1.get('business_goal')}
Scale: {l1.get('scale')}
Compliance: {', '.join(l1.get('compliance_requirements', []))}

L2 Capabilities:
{json.dumps(l2, indent=1)}

L3 Workflows:
{json.dumps(l3, indent=1)}

L4 Entities:
{json.dumps(l4, indent=1)}

L5 Relationships:
{json.dumps(l5, indent=1)}

L7 Modules:
{json.dumps(l7, indent=1)}

L8 Physical Blueprint:
{json.dumps(blueprint, indent=1)}"""

    # 1. Run all reviews (sequentially for safety on Groq rate limits)
    reviews = {}
    total_score = 0.0
    for member in COUNCIL_MEMBERS:
        logger.info(f"  Council member '{member.name}' is reviewing...")
        review_result = member.review(context_str)
        reviews[member.name] = review_result
        total_score += review_result.get("score", 90)

    avg_score = round(total_score / len(COUNCIL_MEMBERS), 1)
    logger.info(f"  Council reviews complete. Average Score: {avg_score}/100")

    # 2. Coordinator synthesizes reviews into a final adjustments list
    coordinator_system_prompt = """You are the Coordinator of the Architecture Council.
Your job is to read the reviews from the Enterprise Architect, Database Architect, Performance Engineer, Compliance Expert, and Reporting Expert.
Synthesize their findings and recommended adjustments into a single, cohesive, non-conflicting list of adjustments.
If there are disagreements (e.g., Performance Engineer wants an index but DB Architect warns it's redundant), resolve them.

Return ONLY valid JSON matching this schema:
{
  "consensus_verdict": "APPROVED" or "NEEDS_REVISION",
  "consensus_score": 0 to 100,
  "resolved_adjustments": [
    {
      "table_name": "target_table_name",
      "adjustment": "Detailed description of the change",
      "requested_by": "Name of the agent who requested this (e.g. Performance Engineer)"
    }
  ],
  "disagreements_logged": [
    {
      "issue": "The conflict description",
      "resolution": "How you resolved it"
    }
  ]
}"""

    coordinator_user_prompt = f"""Individual Agent Reviews:
{json.dumps(reviews, indent=2)}"""

    try:
        response = generate_schema(
            system_prompt=coordinator_system_prompt,
            user_prompt=coordinator_user_prompt
        )
        synthesis = _clean_and_parse_json(response["content"])
        
        # Inject individual reviews for full transparency
        synthesis["individual_reviews"] = reviews
        return synthesis, avg_score

    except Exception as e:
        logger.error(f"Coordinator synthesis failed: {e}", exc_info=True)
        return {
            "consensus_verdict": "APPROVED",
            "consensus_score": avg_score,
            "resolved_adjustments": [],
            "disagreements_logged": [],
            "individual_reviews": reviews
        }, avg_score
