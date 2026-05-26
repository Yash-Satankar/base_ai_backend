# app/api/routes/rules.py

from fastapi import APIRouter, HTTPException
from app.schemas.planner_schemas import SearchRulesRequest, SearchRulesResponse
from app.db.vector_store import search_rules, get_collection_info
from app.services.rule_service import get_collection_stats

router = APIRouter()


@router.get("/")
async def list_rules_info():
    """Get stats about the rules collection."""
    try:
        stats = get_collection_stats()
        return {
            "success": True,
            "stats": stats,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/search", response_model=SearchRulesResponse)
async def search_rules_endpoint(body: SearchRulesRequest):
    """
    Search for relevant rules by query text.
    Use this to test what rules a requirement will trigger.
    """
    try:
        results = search_rules(
            query=body.query,
            top_k=body.top_k,
            category_filter=body.category,
        )
        return SearchRulesResponse(
            success=True,
            query=body.query,
            total_results=len(results),
            rules=results,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))