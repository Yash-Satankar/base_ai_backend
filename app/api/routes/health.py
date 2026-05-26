# app/api/routes/health.py

from fastapi import APIRouter
from app.db.vector_store import get_collection_info
from app.core.config import settings

router = APIRouter()


@router.get("/")
async def health_check():
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "ai_provider": settings.AI_PROVIDER,
    }


@router.get("/qdrant")
async def qdrant_health():
    try:
        info = get_collection_info()
        return {
            "status": "connected",
            "collection": info,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }

@router.get("/full")
async def full_health_check():
    checks = {}

    # Check Qdrant
    try:
        info = get_collection_info()
        checks["qdrant"] = {"status": "ok", "rules": info["total_rules"]}
    except Exception as e:
        checks["qdrant"] = {"status": "error", "message": str(e)}

    # Check Groq
    try:
        from app.services.ai_service import get_groq_client
        client = get_groq_client()
        checks["groq"] = {"status": "ok"}
    except Exception as e:
        checks["groq"] = {"status": "error", "message": str(e)}

    # Check Redis (when you add it)
    # checks["redis"] = ...

    all_ok = all(v["status"] == "ok" for v in checks.values())

    return {
        "status": "healthy" if all_ok else "degraded",
        "checks": checks,
    }