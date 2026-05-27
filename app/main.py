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
from app.api.routes import ai, health, planner, rules, conversation

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

# ── Request logging middleware ────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = round(time.time() - start, 3)

    logger.info(
        f"{request.method} {request.url.path} | "
        f"status={response.status_code} | "
        f"duration={duration}s | "
        f"ip={request.client.host if request.client else 'unknown'}"
    )
    return response

# ── Global error handler ─────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(
        f"Unhandled error on {request.method} {request.url.path}: {exc}",
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "An internal error occurred. Please try again.",
        },
    )

# ── Routes ────────────────────────────────────────────────────────
app.include_router(health.router,        prefix="/health",       tags=["Health"])
app.include_router(rules.router,         prefix="/rules",        tags=["Rules"])
app.include_router(ai.router,            prefix="/ai",           tags=["AI"])
app.include_router(planner.router,       prefix="/planner",      tags=["Planner"])
app.include_router(conversation.router,  prefix="/conversation", tags=["Conversation"])

# ── Startup ───────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
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