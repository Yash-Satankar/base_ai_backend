# app/engine/learning_proposal_engine.py
"""
Learning Proposal Engine: Aggregates user feedback from database logs,
identifies repeating patterns, and generates rules proposals.
"""

import logging
from typing import List
from sqlalchemy import select, func, cast, Integer
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import RecommendationFeedback, LearningProposal

logger = logging.getLogger(__name__)


async def process_feedback_and_generate_proposals(db: AsyncSession) -> List[LearningProposal]:
    """
    Scans the RecommendationFeedback table, groups by item_name,
    and generates LearningProposals for patterns with high acceptance rates.
    """
    logger.info("🤖 Scanning recommendation feedback for learning opportunities...")

    # Query to count actions grouped by item_name and type
    stmt = (
        select(
            RecommendationFeedback.item_name,
            RecommendationFeedback.recommendation_type,
            func.count(RecommendationFeedback.id).label("total_count"),
            func.sum(
                cast(RecommendationFeedback.action == "accepted", Integer)
            ).label("accepted_count")
        )
        .group_by(RecommendationFeedback.item_name, RecommendationFeedback.recommendation_type)
        .having(func.count(RecommendationFeedback.id) >= 3) # Pattern threshold: at least 3 interactions
    )

    res = await db.execute(stmt)
    rows = res.all()

    new_proposals = []

    for row in rows:
        item_name = row.item_name
        rec_type = row.recommendation_type
        total = row.total_count
        accepted = row.accepted_count or 0
        
        acceptance_rate = accepted / total
        logger.info(f"Pattern detected: '{item_name}' ({rec_type}) | Acceptance: {accepted}/{total} ({acceptance_rate:.2%})")

        # Threshold: > 80% acceptance rate
        if acceptance_rate >= 0.8:
            # Check if a proposal already exists
            exist_stmt = select(LearningProposal).where(
                LearningProposal.pattern_type == "table_association",
                LearningProposal.suggested_rule["item_name"].as_string() == item_name
            )
            exist_res = await db.execute(exist_stmt)
            if exist_res.scalar_one_or_none():
                continue

            # Create a new learning proposal
            proposal = LearningProposal(
                pattern_type="table_association",
                suggested_rule={
                    "item_name": item_name,
                    "recommendation_type": rec_type,
                    "reason": f"Automatically proposed due to {acceptance_rate:.0%} acceptance rate across {total} projects."
                },
                confidence_score=round(acceptance_rate, 2),
                status="pending"
            )
            db.add(proposal)
            new_proposals.append(proposal)
            logger.info(f"💡 Generated new Learning Proposal for: {item_name}")

    if new_proposals:
        await db.flush()

    return new_proposals
