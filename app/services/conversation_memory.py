# app/services/conversation_memory.py
"""
Durable conversation memory (Phase 3).

- ``rehydrate``  — on ``create_session(project_id=...)``, load the project's
  ConversationMemory and warm-start the fresh session with the rolling
  summary, decisions, facts, and last blueprint.
- ``persist_checkpoint`` — write the distillation back at checkpoints:
  blueprint confirmed, schema complete, session end.

Nothing here may break a turn: every path is best-effort and logged.
"""

import dataclasses
import logging

from app.db.repositories.memory_repo import ConversationMemoryRepository
from app.engine.conversation_engine import ConversationStage, ProjectBlueprint

logger = logging.getLogger(__name__)

# per-turn caches that should NOT survive into durable memory
_TRANSIENT_FACT_KEYS = {
    "_domain_for", "_all_domains", "_domain_conf", "_summarized_upto",
    "_lang_ack_done", "_resumed", "_resume_summary",
}


def _durable_facts(facts: dict) -> dict:
    return {k: v for k, v in (facts or {}).items() if k not in _TRANSIENT_FACT_KEYS}


def _blueprint_to_dict(bp) -> dict | None:
    if bp is None:
        return None
    try:
        return dataclasses.asdict(bp)
    except Exception:
        return getattr(bp, "__dict__", None)


def _blueprint_from_dict(data: dict) -> ProjectBlueprint | None:
    if not data:
        return None
    try:
        known = {f.name for f in dataclasses.fields(ProjectBlueprint)}
        return ProjectBlueprint(**{k: v for k, v in data.items() if k in known})
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"conversation_memory: could not rebuild blueprint ({e})")
        return None


async def rehydrate(state, db, project_id: str) -> bool:
    """Warm-start ``state`` from the project's durable memory. Returns True if
    memory was found and applied."""
    if not (db and project_id):
        return False
    try:
        repo = ConversationMemoryRepository(db)
        mem = await repo.get_by_project(project_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"conversation_memory: rehydrate lookup failed ({e})")
        return False

    if mem is None:
        return False

    state.rolling_summary = mem.rolling_summary or ""
    state.requirement_summary = mem.requirement_summary or ""
    state.key_decisions = list(mem.key_decisions or [])
    state.rejected_options = list(mem.rejected_options or [])
    state.understood_aspects = dict(mem.understood_aspects or {})
    state.facts.update(_durable_facts(mem.facts or {}))
    state.facts["_resumed"] = True

    bp = _blueprint_from_dict(mem.last_blueprint)
    if bp is not None:
        state.blueprint = bp
        state.stage = ConversationStage.BLUEPRINT
        state.facts["_resume_summary"] = (
            f"Welcome back — I've picked up **{bp.project_name}** where we left off. "
            f"The blueprint is still here: say **YES** to build the SQL, or tell me what to change."
        )
    elif state.requirement_summary:
        state.stage = ConversationStage.CLARIFYING
        state.facts["_resume_summary"] = (
            "Welcome back — I've still got the context from our last session. "
            "Add anything new, or say **Generate Blueprint** when you're ready."
        )

    logger.info(
        f"🧠 Rehydrated project {project_id}: "
        f"{len(state.key_decisions)} decisions, blueprint={'yes' if bp else 'no'}, "
        f"stage={state.stage}"
    )
    return True


async def persist_checkpoint(state, db, *, reason: str, commit: bool = True) -> bool:
    """Upsert the project's durable memory from ``state``. Best-effort."""
    if not (db and getattr(state, "project_id", None)):
        return False
    try:
        repo = ConversationMemoryRepository(db)
        await repo.upsert(
            state.project_id,
            rolling_summary=state.rolling_summary or None,
            requirement_summary=state.requirement_summary or None,
            key_decisions=list(state.key_decisions or []),
            rejected_options=list(state.rejected_options or []),
            facts=_durable_facts(state.facts),
            understood_aspects=dict(state.understood_aspects or {}),
            last_blueprint=_blueprint_to_dict(state.blueprint),
            last_checkpoint=reason,
        )
        if commit:
            await db.commit()
        logger.info(f"💾 Conversation memory checkpoint '{reason}' saved for project {state.project_id}")
        return True
    except Exception as e:
        logger.warning(f"conversation_memory: checkpoint '{reason}' failed ({e})")
        try:
            if commit:
                await db.rollback()
        except Exception:
            pass
        return False
