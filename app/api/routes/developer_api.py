# app/api/routes/developer_api.py
"""
Developer API Router: Exposes public endpoints for CI/CD integrations,
automated SQL reviews, and marketplace package queries.
"""

import logging
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.database import get_db
from app.services.architecture_reviewer import review_external_sql
from app.engine.time_machine import generate_migration_plan

from app.core.security import verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(verify_api_key)])


# ── Request/Response Schemas ──────────────────────────────────────────────────

class SQLReviewRequest(BaseModel):
    sql_content: str
    scale: Optional[str] = "medium"

class MigrationRequest(BaseModel):
    old_blueprint: dict
    new_blueprint: dict


# ── API Endpoints ─────────────────────────────────────────────────────────────

@router.post("/review")
async def review_sql_schema(req: SQLReviewRequest):
    """
    Accepts raw SQL DDL and returns a comprehensive health, compliance,
    and performance review. Excellent for Git commit hooks.
    """
    if not req.sql_content.strip():
        raise HTTPException(status_code=400, detail="SQL content cannot be empty.")
    
    try:
        report = review_external_sql(req.sql_content, req.scale)
        return report
    except Exception as e:
        logger.error(f"Failed to review SQL schema: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal review error: {str(e)}")


@router.post("/migrate")
async def compile_migration(req: MigrationRequest):
    """
    Compares two logical blueprints and returns the compiled DDL migration script.
    """
    try:
        plan = generate_migration_plan(req.old_blueprint, req.new_blueprint)
        return {
            "success": True,
            "migration_sql": plan["migration_sql"],
            "added_tables": plan["added_tables"],
            "removed_tables": plan["removed_tables"],
            "modified_tables": plan["modified_tables"]
        }
    except Exception as e:
        logger.error(f"Failed to compile migration: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal migration compiler error: {str(e)}")


@router.get("/packages")
async def get_marketplace_packages(db: AsyncSession = Depends(get_db)):
    """
    Lists all public architecture packages in the marketplace.
    """
    # Simply return a set of predefined packages for the marketplace MVP
    return {
        "success": True,
        "packages": [
            {
                "name": "LedgerCore",
                "version": "1.2.0",
                "description": "Double-entry financial ledger component with audit mirroring.",
                "downloads": 1250,
                "rating": 4.9
            },
            {
                "name": "ApprovalCore",
                "version": "1.0.4",
                "description": "Multi-stage role-based approval engine workflow logging.",
                "downloads": 840,
                "rating": 4.7
            },
            {
                "name": "AuditCore",
                "version": "2.1.0",
                "description": "System-wide transaction and change data capture logging.",
                "downloads": 3100,
                "rating": 4.9
            }
        ]
    }
