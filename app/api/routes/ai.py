# app/api/routes/ai.py

from fastapi import APIRouter
from app.core.config import settings

router = APIRouter()


@router.get("/provider")
async def get_ai_provider():
    """Check which AI provider is currently active."""
    return {
        "active_provider": settings.AI_PROVIDER,
        "groq_configured": bool(settings.GROQ_API_KEY),
        # "anthropic_configured": bool(settings.ANTHROPIC_API_KEY),
        "model": settings.GROQ_MODEL if settings.AI_PROVIDER == "groq" else "not active",
        "max_tokens": settings.MAX_TOKENS,
        "top_k_rules": settings.TOP_K_RULES,
    }