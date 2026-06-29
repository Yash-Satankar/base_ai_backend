# app/api/routes/learning.py
"""
Learning API Router: Exposes endpoints for recording recommendation feedback,
managing learning proposals, and retrieving organization style memories.
"""

import logging
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.database import get_db
from app.db.models import RecommendationFeedback, LearningProposal, OrganizationMemory
from app.engine.learning_proposal_engine import process_feedback_and_generate_proposals

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Request/Response Schemas ──────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    project_id: str
    recommendation_type: str
    item_name: str
    action: str  # "accepted" | "modified" | "rejected" | "ignored"
    user_id: str


# ── API Endpoints ─────────────────────────────────────────────────────────────

@router.post("/feedback")
async def record_feedback(req: FeedbackRequest, db: AsyncSession = Depends(get_db)):
    """
    Records a user's action on an AI recommendation.
    Triggers the learning engine to evaluate new proposals.
    """
    try:
        feedback = RecommendationFeedback(
            project_id=req.project_id,
            recommendation_type=req.recommendation_type,
            item_name=req.item_name,
            action=req.action,
            user_id=req.user_id
        )
        db.add(feedback)
        await db.flush()

        # Run proposal check
        new_proposals = await process_feedback_and_generate_proposals(db)
        await db.commit()

        return {
            "success": True,
            "message": "Feedback recorded successfully.",
            "proposals_generated": len(new_proposals)
        }
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to record feedback: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal learning error: {str(e)}")


@router.get("/proposals")
async def get_learning_proposals(db: AsyncSession = Depends(get_db)):
    """
    Lists all pending learning proposals.
    """
    try:
        stmt = select(LearningProposal).where(LearningProposal.status == "pending")
        res = await db.execute(stmt)
        proposals = res.scalars().all()
        return {
            "success": True,
            "proposals": [
                {
                    "id": p.id,
                    "pattern_type": p.pattern_type,
                    "suggested_rule": p.suggested_rule,
                    "confidence_score": p.confidence_score,
                    "status": p.status,
                    "created_at": p.created_at
                }
                for p in proposals
            ]
        }
    except Exception as e:
        logger.error(f"Failed to fetch proposals: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal learning error: {str(e)}")


@router.post("/proposals/{proposal_id}/approve")
async def approve_learning_proposal(proposal_id: str, db: AsyncSession = Depends(get_db)):
    """
    Approves a learning proposal, promoting it to active status.
    """
    try:
        stmt = select(LearningProposal).where(LearningProposal.id == proposal_id)
        res = await db.execute(stmt)
        proposal = res.scalar_one_or_none()

        if not proposal:
            raise HTTPException(status_code=404, detail="Learning proposal not found.")

        proposal.status = "approved"
        await db.commit()

        logger.info(f"✅ Approved learning proposal {proposal_id[:8]}. Rule promoted.")
        return {"success": True, "message": "Proposal approved and promoted."}
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to approve proposal: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal learning error: {str(e)}")


@router.get("/org-memory/{org_id}")
async def get_org_memory(org_id: str, db: AsyncSession = Depends(get_db)):
    """
    Retrieves the organization's specific naming style and preferences.
    """
    try:
        stmt = select(OrganizationMemory).where(OrganizationMemory.org_id == org_id)
        res = await db.execute(stmt)
        org_mem = res.scalar_one_or_none()

        if not org_mem:
            # Create a default org memory record for the MVP
            org_mem = OrganizationMemory(
                org_id=org_id,
                naming_style="suffix_header_all",
                preferred_components={"LedgerCore": "1.2.0", "AuditCore": "2.1.0"}
            )
            db.add(org_mem)
            await db.commit()

        return {
            "success": True,
            "org_id": org_mem.org_id,
            "naming_style": org_mem.naming_style,
            "preferred_components": org_mem.preferred_components
        }
    except Exception as e:
        logger.error(f"Failed to fetch org memory: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal learning error: {str(e)}")
