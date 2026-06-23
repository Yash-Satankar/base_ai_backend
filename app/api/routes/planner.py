# app/api/routes/planner.py

from fastapi import APIRouter, HTTPException, Depends, Request
from app.core.auth import verify_api_key
from app.schemas.planner_schemas import (
    GenerateSchemaRequest,
    GenerateSchemaResponse,
    MatchRulesRequest,
    MatchRulesResponse,
    GenerateBlueprintRequest,
    GenerateBlueprintResponse,
)
from app.services.planner_service import (
    generate_database_schema,
    get_matched_rules_only,
)
from app.engine.architecture_planner import generate_deep_blueprint
from app.engine.rule_matcher import detect_domain
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
        result = generate_database_schema(
            requirement=clean_req,
            blueprint=body.blueprint,
            additional_context=body.additional_context,
        )
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


@router.post("/blueprint", response_model=GenerateBlueprintResponse)
@limiter.limit("10/minute")
async def generate_blueprint_endpoint(
    request: Request,
    body: GenerateBlueprintRequest,
):
    """
    Generate an architectural blueprint (modules and tables) before generating schema.
    """
    clean_req = sanitise_input(body.requirement)
    
    # Auto-detect domain if not provided
    domain = body.domain
    if not domain:
        domain, _ = detect_domain(clean_req)
        
    # Auto-detect GST requirement if not provided
    gst_required = body.gst_required
    if gst_required is None:
        gst_required = any(w in clean_req.lower() for w in ["gst", "invoice", "tax", "billing"])
        
    scale = body.scale or "medium"
    
    try:
        result = generate_deep_blueprint(
            requirement=clean_req,
            domain=domain,
            gst_required=gst_required,
            scale=scale,
        )
        return GenerateBlueprintResponse(success=True, **result)
    except Exception as e:
        logger.error(f"Blueprint generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Blueprint generation failed.")