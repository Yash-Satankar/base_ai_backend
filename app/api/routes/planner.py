# app/api/routes/planner.py

import asyncio
import logging
from fastapi import APIRouter, HTTPException, Request

from app.core.security import limiter, sanitise_input
from app.schemas.planner_schemas import (
    GenerateSchemaRequest,
    GenerateSchemaResponse,
    MatchRulesRequest,
    MatchRulesResponse,
    GenerateBlueprintRequest,
    GenerateBlueprintResponse,
    SubmitJobResponse,
    JobStatusResponse,
)
from app.services.planner_service import (
    generate_database_schema,
    generate_database_schema_for_job,
    get_matched_rules_only,
)
from app.services.job_store import get_job_store
from app.engine.architecture_planner import generate_deep_blueprint
from app.engine.rule_matcher import detect_domain

logger = logging.getLogger(__name__)
router = APIRouter()


# ── POST /planner/generate  (async — returns job_id immediately) ──

@router.post("/generate", response_model=SubmitJobResponse)
@limiter.limit("5/minute")
async def generate_schema_endpoint(
    request: Request,
    body: GenerateSchemaRequest,
):
    """
    Submit a schema generation job.  Returns immediately with a job_id.
    Poll GET /planner/job/{job_id} for progress and the final result.

    This endpoint never blocks — generation runs in a background thread.
    """
    clean_req = sanitise_input(body.requirement)

    store  = get_job_store()
    job_id = store.create(clean_req, body.blueprint)

    # Fire-and-forget in the thread pool so the event loop is never blocked
    asyncio.create_task(
        asyncio.to_thread(
            generate_database_schema_for_job,
            job_id,
            clean_req,
            body.blueprint,
            body.additional_context,
            body.session_id,
        )
    )

    logger.info(f"📥 Job queued: {job_id[:8]}... | req: {clean_req[:60]}")

    return SubmitJobResponse(
        success=True,
        job_id=job_id,
        status="queued",
        poll_url=f"/planner/job/{job_id}",
    )


# ── GET /planner/job/{job_id}  (polling endpoint) ─────────────────

@router.get("/job/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    """
    Poll this endpoint to check progress or retrieve the finished schema.

    Response `status` values:
    - `queued`     — job is waiting to start
    - `generating` — generation is in progress (check `progress` field)
    - `done`       — complete; full schema is in `result`
    - `failed`     — generation failed; see `error` field
    """
    store = get_job_store()
    job   = store.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    return JobStatusResponse(
        success=True,
        job_id=job_id,
        status=job["status"],
        progress=job.get("progress"),
        result=job.get("result"),          # None until done
        error=job.get("error"),
        created_at=job.get("created_at"),
        started_at=job.get("started_at"),
        completed_at=job.get("completed_at"),
    )


# ── POST /planner/generate-sync  (synchronous — for testing only) ─

@router.post("/generate-sync", response_model=GenerateSchemaResponse)
@limiter.limit("3/minute")
async def generate_schema_sync_endpoint(
    request: Request,
    body: GenerateSchemaRequest,
):
    """
    Synchronous schema generation — blocks until complete.

    ⚠️  WARNING: This will time out for large projects (10+ modules).
    Use POST /generate instead for production workloads.
    Kept here for local testing and small single-module requests.
    """
    clean_req = sanitise_input(body.requirement)
    try:
        result = await asyncio.to_thread(
            generate_database_schema,
            clean_req,
            body.blueprint,
            body.additional_context,
        )
        return GenerateSchemaResponse(success=True, **result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Sync generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Schema generation failed.")


# ── POST /planner/match-rules ─────────────────────────────────────

@router.post("/match-rules", response_model=MatchRulesResponse)
async def match_rules_endpoint(body: MatchRulesRequest):
    """
    Dry run — shows which rules would be applied WITHOUT generating schema.
    Use this to debug or show users which rules are active.
    """
    try:
        result = get_matched_rules_only(body.requirement)
        return MatchRulesResponse(success=True, **result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── POST /planner/blueprint ───────────────────────────────────────

@router.post("/blueprint", response_model=GenerateBlueprintResponse)
@limiter.limit("10/minute")
async def generate_blueprint_endpoint(
    request: Request,
    body: GenerateBlueprintRequest,
):
    """
    Generate an architectural blueprint (modules + tables list) before
    generating the actual SQL schema.  Use the returned blueprint as
    input to POST /generate to guide generation.
    """
    clean_req = sanitise_input(body.requirement)

    # Auto-detect domain if not provided
    domain = body.domain
    if not domain:
        domain, _ = detect_domain(clean_req)

    # Auto-detect GST requirement if not provided
    gst_required = body.gst_required
    if gst_required is None:
        gst_required = any(
            w in clean_req.lower()
            for w in ["gst", "invoice", "tax", "billing"]
        )

    scale = body.scale or "medium"

    try:
        result = await asyncio.to_thread(
            generate_deep_blueprint,
            clean_req,
            domain,
            gst_required,
            scale,
        )
        return GenerateBlueprintResponse(success=True, **result)
    except Exception as e:
        logger.error(f"Blueprint generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Blueprint generation failed.")


# ── GET /planner/jobs/stats ───────────────────────────────────────

@router.get("/jobs/stats")
async def job_stats():
    """Returns a count of jobs by status — useful for monitoring."""
    store = get_job_store()
    return {"success": True, "stats": store.count()}