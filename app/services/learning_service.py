# app/services/learning_service.py
"""
The Learning Service: Drives continuous self-improvement and architectural memory.
Analyzes completed designs to extract reusable patterns, identify redundancies,
and recommend new platform components.
"""

import logging
import json
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import ProjectVersion, VersionStatus
from app.services.ai_service import generate_schema
from app.engine.abstraction_pipeline import _clean_and_parse_json

logger = logging.getLogger(__name__)


async def run_self_improvement_loop(version_id: str, db: AsyncSession) -> dict:
    """
    Runs the post-generation self-improvement analysis.
    1. Fetches the current version's design.
    2. Fetches recent historical versions for comparison.
    3. Triggers AI analysis to extract patterns and recommend platform optimizations.
    """
    logger.info(f"🔄 Running self-improvement loop for version: {version_id}")
    
    # 1. Fetch current version
    current_version_query = await db.execute(
        select(ProjectVersion).where(ProjectVersion.id == version_id)
    )
    current_version = current_version_query.scalar_one_or_none()
    if not current_version:
        logger.error(f"Version {version_id} not found for self-improvement.")
        return {}

    # 2. Fetch up to 5 recent completed versions across other projects
    historical_query = await db.execute(
        select(ProjectVersion)
        .where(
            ProjectVersion.status == VersionStatus.COMPLETE,
            ProjectVersion.id != version_id
        )
        .order_by(ProjectVersion.created_at.desc())
        .limit(5)
    )
    historical_versions = historical_query.scalars().all()

    # 3. Format context for the LLM
    current_design = {
        "project_name": current_version.project_name if hasattr(current_version, "project_name") else "Current Project",
        "domain": current_version.domain,
        "entities": current_version.l4_entities,
        "blueprint": current_version.blueprint
    }

    past_designs = []
    for hv in historical_versions:
        past_designs.append({
            "project_name": f"Project (domain: {hv.domain})",
            "domain": hv.domain,
            "entities": hv.l4_entities,
            "blueprint": hv.blueprint
        })

    system_prompt = """You are the Principal Enterprise Architect and Platform Optimizer.
Analyze the current database design and compare it with recent historical designs.
Identify architectural memory overlaps, extract reusable patterns, and recommend optimizations to make our platform smarter.

Specifically:
1. 'pattern_discovery': Look for commonalities. E.g., if this project has scheduling and previous ones did too, recommend converging them into a unified 'Scheduling Engine'.
2. 'redundancy_report': Identify any duplicate or unnecessarily complex tables/columns.
3. 'reusable_component_recommendations': Recommend new configurable Architecture Components that should be added to our registry.
4. 'validation_rules_recommendations': Recommend new validation rules that should be added to our validator.
5. 'simplification_opportunities': Ideas to simplify future designs in this domain.

Return ONLY valid JSON matching this schema:
{
  "discovered_patterns": [
    {
      "pattern_name": "Unified Booking Pattern",
      "overlap_detected": "Overlap between Salon Booking in Project A and Doctor Appointment in Project B",
      "recommended_action": "Converge into a unified Scheduling Engine with columns X, Y, Z"
    }
  ],
  "reusable_components": [
    {
      "component_name": "CalendarBookingEngine",
      "description": "Configurable engine to handle temporal conflicts and slot reservations.",
      "tables": ["slot_header_all", "reservation_transaction_all"]
    }
  ],
  "new_validation_rules": [
    {
      "rule_name": "prevent_inline_booking_status",
      "reason": "Booking status changes should always use a companion _life_cycle_all table for compliance."
    }
  ],
  "simplification_opportunities": [
    "Reduce the number of junction tables in the billing module by using a JSON array for simple tag listings."
  ]
}"""

    user_prompt = f"""Current Project Design:
{json.dumps(current_design, indent=2)}

Recent Past Project Designs:
{json.dumps(past_designs, indent=2)}"""

    try:
        response = generate_schema(
            system_prompt=system_prompt,
            user_prompt=user_prompt
        )
        recommendations = _clean_and_parse_json(response["content"])
        
        # Save recommendations into the version record's metadata / diff_summary for traceability
        current_version.diff_summary = {
            **(current_version.diff_summary or {}),
            "self_improvement_recommendations": recommendations
        }
        await db.flush()
        logger.info("✅ Self-improvement analysis complete and saved to database")
        return recommendations
    except Exception as e:
        logger.error(f"❌ Self-improvement loop failed: {e}", exc_info=True)
        return {}
