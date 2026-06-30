# app/api/routes/health.py

from fastapi import APIRouter, Response, status
from sqlalchemy import text
from app.db.vector_store import get_collection_info
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.db.session_store import get_redis_client

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
async def full_health_check(response: Response):
    checks = {}

    # Check PostgreSQL
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        checks["postgres"] = {"status": "ok"}
    except Exception as e:
        checks["postgres"] = {"status": "error", "message": str(e)}

    # Check Redis
    try:
        client = get_redis_client()
        if client:
            client.ping()
            checks["redis"] = {"status": "ok"}
        else:
            checks["redis"] = {"status": "error", "message": "Redis client is None"}
    except Exception as e:
        checks["redis"] = {"status": "error", "message": str(e)}

    # Check Qdrant
    try:
        info = get_collection_info()
        checks["qdrant"] = {"status": "ok", "rules": info.get("total_rules", 0)}
    except Exception as e:
        checks["qdrant"] = {"status": "error", "message": str(e)}

    # Check Groq
    try:
        from app.services.ai_service import get_groq_client
        client = get_groq_client()
        checks["groq"] = {"status": "ok"}
    except Exception as e:
        checks["groq"] = {"status": "error", "message": str(e)}

    all_ok = all(v["status"] == "ok" for v in checks.values())

    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "healthy" if all_ok else "degraded",
        "checks": checks,
    }