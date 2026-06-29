# app/services/org_memory_service.py
"""
Organization Memory Service: Retrieves organization-specific naming styles
and package preferences, injecting them directly into the system prompt.
"""

import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import OrganizationMemory

logger = logging.getLogger(__name__)


async def inject_org_preferences(
    db: AsyncSession,
    org_id: str,
    system_prompt: str
) -> str:
    """
    Queries the OrganizationMemory for the specified org_id,
    and appends styling instructions to the system prompt.
    """
    logger.info(f"🧠 Checking Organization Memory for org: {org_id}...")

    stmt = select(OrganizationMemory).where(OrganizationMemory.org_id == org_id)
    res = await db.execute(stmt)
    org_mem = res.scalar_one_or_none()

    if not org_mem:
        logger.info(f"No custom memory found for org {org_id}. Using standard style.")
        return system_prompt

    # Construct custom style instructions
    custom_instructions = ["\n\n=== ORGANIZATION ARCHITECTURAL STYLE PREFERENCES ==="]
    
    if org_mem.naming_style == "suffix_header_all":
        custom_instructions.append(
            "- Always suffix all primary master entity tables with `_header_all` (e.g., `user_header_all`, `product_header_all`).\n"
            "- Always suffix transactional/ledger tables with `_transaction_all`.\n"
            "- Always suffix audit tables with `_archive_all`."
        )
    elif org_mem.naming_style == "snake_case":
        custom_instructions.append(
            "- Use strictly snake_case for all table and column names.\n"
            "- Do not use camelCase or PascalCase under any circumstances."
        )
    
    if org_mem.preferred_components:
        preferred_list = ", ".join(f"'{k}' (v{v})" for k, v in org_mem.preferred_components.items())
        custom_instructions.append(
            f"- Prioritize using the following pre-approved packages for reusable modules: {preferred_list}."
        )

    custom_instructions.append("=====================================================")

    injected_prompt = system_prompt + "\n" + "\n".join(custom_instructions)
    logger.info(f"✅ Injected organization style preferences into system prompt.")
    return injected_prompt
