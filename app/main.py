import time
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from app.core.config import settings
from app.core.logger import setup_logging
from app.core.security import limiter
from app.core.logging_middleware import StructuredLoggingMiddleware
from app.api.routes import ai, health, planner, rules, conversation, auth, projects, dashboard, developer_api, validation, learning
from app.db.database import init_db

# ── Logging ──────────────────────────────────────────────────────
setup_logging(debug=settings.DEBUG)
logger = logging.getLogger(__name__)

# ── App ──────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI-powered MySQL schema generator",
    docs_url="/docs" if settings.DEBUG else None,       # hide docs in prod
    redoc_url="/redoc" if settings.DEBUG else None,
)

# ── Structured Logging Middleware ─────────────────────────────────
app.add_middleware(StructuredLoggingMiddleware)

# ── Rate limiting ─────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ─────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# ── Global error handler ─────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    # StructuredLoggingMiddleware will catch and log the full traceback as JSON.
    # Here we return a clean, user-friendly response with the request_id.
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "An internal server error occurred. Please try again.",
            "request_id": request_id,
        },
    )

# ── Routes ────────────────────────────────────────────────────────
app.include_router(health.router,        prefix="/health",       tags=["Health"])
app.include_router(rules.router,         prefix="/rules",        tags=["Rules"])
app.include_router(ai.router,            prefix="/ai",           tags=["AI"])
app.include_router(planner.router,       prefix="/planner",      tags=["Planner"])
app.include_router(conversation.router,  prefix="/conversation", tags=["Conversation"])
app.include_router(auth.router,          prefix="/auth",         tags=["Auth"])
app.include_router(projects.router,      prefix="/projects",     tags=["Projects"])
app.include_router(dashboard.router,     prefix="/dashboard",    tags=["Dashboard"])
app.include_router(developer_api.router, prefix="/api/v1",       tags=["Developer API"])
app.include_router(validation.router,    prefix="/validation",   tags=["Validation"])
app.include_router(learning.router,      prefix="/learning",     tags=["Learning"])

# ── Startup ───────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    # Initialize the PostgreSQL metadata database
    await init_db()

    # Run strict startup checks in production
    if not settings.DEBUG:
        logger.info("🔍 Running production startup health checks...")
        from sqlalchemy import text
        from app.db.database import AsyncSessionLocal
        from app.db.session_store import get_redis_client
        from app.db.vector_store import get_collection_info

        # 1. Test Postgres
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(text("SELECT 1"))
            logger.info("  ✓ Postgres connection OK")
        except Exception as e:
            logger.critical(f"❌ Postgres connection failed on startup: {e}")
            raise RuntimeError(f"Postgres connection failed: {e}")

        # 2. Test Redis
        try:
            client = get_redis_client()
            if not client:
                raise ValueError("Redis client is None")
            client.ping()
            logger.info("  ✓ Redis connection OK")
        except Exception as e:
            logger.critical(f"❌ Redis connection failed on startup: {e}")
            raise RuntimeError(f"Redis connection failed: {e}")

        # 3. Test Qdrant
        try:
            get_collection_info()
            logger.info("  ✓ Qdrant connection OK")
        except Exception as e:
            logger.critical(f"❌ Qdrant connection failed on startup: {e}")
            raise RuntimeError(f"Qdrant connection failed: {e}")
    
    logger.info(f"🚀 {settings.APP_NAME} v{settings.APP_VERSION} started")
    logger.info(f"🤖 AI provider: {settings.AI_PROVIDER}")
    logger.info(f"📦 Qdrant: {settings.QDRANT_COLLECTION_NAME}")
    logger.info(f"🔒 Docs: {'enabled' if settings.DEBUG else 'disabled'}")

# ── Root ──────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "app":     settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status":  "running",
    }