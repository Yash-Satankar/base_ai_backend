# app/api/routes/planner.py

from fastapi import APIRouter, HTTPException, Depends, Request
from app.core.auth import verify_api_key
from app.schemas.planner_schemas import (
    GenerateSchemaRequest,
    GenerateSchemaResponse,
    MatchRulesRequest,
    MatchRulesResponse,
)
from app.services.planner_service import (
    generate_database_schema,
    get_matched_rules_only,
)
import logging
from app.core.security import limiter, verify_api_key, sanitise_input

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/generate", response_model=GenerateSchemaResponse)
@limiter.limit("5/minute")
async def generate_schema_endpoint(
    request: Request,
    body: GenerateSchemaRequest,
    # api_key: str = Depends(verify_api_key),
):
    clean_req = sanitise_input(body.requirement)
    try:
        result = generate_database_schema(requirement=clean_req)
        return GenerateSchemaResponse(success=True, **result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Schema generation failed.")


@router.post("/match-rules", response_model=MatchRulesResponse)
async def match_rules_endpoint(body: MatchRulesRequest):
    """
    Dry run — shows which rules would be applied WITHOUT generating schema.
    Use this to debug or show users which rules are active.
    """
    try:
        result = get_matched_rules_only(body.requirement)
        return MatchRulesResponse(
            success=True,
            **result,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))