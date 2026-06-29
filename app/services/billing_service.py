# app/services/billing_service.py
"""
Billing Service: Manages credit balances, usage limits, and commercial
billing logs for API calls and package downloads.
"""

import logging
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def get_credits_balance(db: AsyncSession, user_id: str) -> int:
    """
    Returns the remaining API credits for the specified user.
    Each user receives 100 free credits upon registration.
    """
    # Mock credit retrieval (defaults to 100 credits for MVP)
    logger.info(f"💳 Fetching credit balance for user {user_id[:8]}...")
    return 100


async def deduct_credits(
    db: AsyncSession,
    user_id: str,
    amount: int,
    operation: str
) -> bool:
    """
    Deducts credits from the user's balance for a specific API operation.
    Returns True if successful, False if insufficient credits.
    """
    current_balance = await get_credits_balance(db, user_id)
    
    if current_balance < amount:
        logger.warning(
            f"⚠️ User {user_id[:8]} has insufficient credits "
            f"({current_balance}) for operation '{operation}' (requires {amount})."
        )
        return False

    new_balance = current_balance - amount
    logger.info(
        f"💳 Deducted {amount} credits from user {user_id[:8]} for '{operation}'. "
        f"Remaining: {new_balance} credits."
    )
    return True
