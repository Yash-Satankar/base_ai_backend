# app/api/routes/planner.py

from fastapi import APIRouter, HTTPException, Depends
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

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/generate", response_model=GenerateSchemaResponse)
@limiter.limit("5/minute")
async def generate_schema_endpoint(body: GenerateSchemaRequest, key=Depends(verify_api_key)):
    """
    Main endpoint — generates complete MySQL schema from requirement.

    Steps internally:
    1. Detects domain
    2. Matches relevant rules from Qdrant
    3. Injects rules into AI prompt
    4. Generates schema
    5. Returns schema + metadata
    """
    try:
        result = generate_database_schema(
            requirement=body.requirement,
            additional_context=body.additional_context,
        )
        return GenerateSchemaResponse(
            success=True,
            schema=result["schema"],
            metadata=result["metadata"],
            validation=result.get("validation"),
        )
    except Exception as e:
        logger.error(f"Schema generation failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


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