# app/conversation/blueprint_job.py
"""
Async L1-L8 blueprint compile.

Phase 2 moves the 5-call abstraction pipeline out of the synchronous
`/conversation/message` request. It now runs as a job (same job-store
pattern as schema generation), attributed to the conversation for cost
tracking, and writes the finished blueprint back into the session.
"""

import logging

from app.services.job_store import get_job_store
from app.db.session_store import load_session, save_session
from app.engine.conversation_engine import ConversationStage
from app.conversation import llm_client

logger = logging.getLogger(__name__)


def _derive_gst_scale(requirement: str) -> tuple[bool, str]:
    low = requirement.lower()
    gst = any(w in low for w in ("gst", "invoice", "tax", "billing"))
    scale = "medium"
    if any(w in low for w in ("large", "million", "millions", "enterprise", "thousands", "billion")):
        scale = "large"
    elif any(w in low for w in ("small", "startup", "simple", "basic")):
        scale = "small"
    return gst, scale


def run_blueprint_job(job_id: str, session_id: str, requirement: str) -> None:
    """Compile L1-L8, store the blueprint on the session, complete the job."""
    store = get_job_store()
    store.mark_started(job_id, modules_total=0, tables_planned=0)

    try:
        state = load_session(session_id)
        if not state:
            store.fail(job_id, "session expired before the blueprint could be compiled")
            return

        # keep imports lazy — conversation_service imports this module's siblings
        from app.services import conversation_service as cs
        from app.engine.rule_matcher import detect_domain

        domain = state.facts.get("_domain")
        if not domain:
            domain, _ = detect_domain(requirement)
        gst_required, scale = _derive_gst_scale(requirement)

        # attribute every L1-L8 call to this conversation's cost budget
        llm_client.set_context(session_id=session_id, project_id=state.project_id)
        try:
            blueprint = cs._compile_pipeline_blueprint(
                state=state,
                requirement=requirement,
                domain=domain,
                gst_required=gst_required,
                scale=scale,
                decomposition_requested=bool(state.decomposition_requested),
            )
        finally:
            llm_client.clear_context()

        state.blueprint = blueprint
        state.stage = ConversationStage.BLUEPRINT

        blueprint_text = cs._format_blueprint(blueprint)
        total_tables = sum(len(m.get("tables", [])) for m in blueprint.modules)
        message = (
            f"I've mapped out the **Database Blueprint** for your project:\n\n"
            f"{blueprint_text}\n\n"
            f"**~{total_tables} tables** across **{len(blueprint.modules)} modules**.\n\n"
            f"---\n"
            f"**Does this match what you have in mind?**\n"
            f"- **YES** to build the full SQL schema\n"
            f"- **EDIT [what to change]** to adjust it\n"
            f"- **ADD [module name]** to include something more"
        )
        state.add_message("assistant", message)
        save_session(state)

        result = {
            "stage": ConversationStage.BLUEPRINT.value,
            "message": message,
            "blueprint": cs._blueprint_to_dict(blueprint),
        }
        store.complete(job_id, result)
        logger.info(f"[blueprint:{job_id[:8]}] done — {total_tables} tables, session {session_id}")

        _persist_blueprint(state)

    except Exception as e:
        logger.error(f"[blueprint:{job_id[:8]}] failed: {e}", exc_info=True)
        store.fail(job_id, str(e))
        state = load_session(session_id)
        if state:
            # return the user to clarifying so they can retry / adjust
            state.stage = ConversationStage.CLARIFYING
            state.add_message(
                "assistant",
                "I hit a snag putting the blueprint together. Tell me to try again, "
                "or add any detail you think would help.",
            )
            save_session(state)


def _persist_blueprint(state) -> None:
    """Best-effort write of the blueprint onto the linked ProjectVersion."""
    if not state.version_id:
        return
    try:
        import asyncio

        async def _write():
            from app.db.database import AsyncSessionLocal
            from app.db.repositories.project_repo import ProjectRepository
            async with AsyncSessionLocal() as db:
                repo = ProjectRepository(db)
                version = await repo.get_version_by_id(state.version_id)
                if version:
                    bp = state.blueprint
                    version.blueprint = bp.__dict__ if hasattr(bp, "__dict__") else bp
                    if not version.requirement_text or version.requirement_text == "Not yet described":
                        version.requirement_text = state.requirement_summary
                    await db.commit()

        asyncio.run(_write())
    except Exception as e:  # pragma: no cover - persistence is best-effort
        logger.warning(f"blueprint_job: could not persist blueprint to PG: {e}")
