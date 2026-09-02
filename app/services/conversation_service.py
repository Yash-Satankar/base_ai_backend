import uuid
import json
import logging
import re
from typing import Optional
from app.engine.conversation_engine import (
    ConversationState,
    ConversationStage,
    ProjectBlueprint,
)
from app.engine.rule_matcher import detect_domain, detect_all_domains
from app.services.ai_service import generate_schema
from app.services.planner_service import generate_database_schema
from app.validators.schema_validator import SchemaValidator
from app.core.config import settings
from app.services.file_service import generate_sql_file, generate_pdf_documentation
from app.engine.intent_detector import detect_intent, IntentType
from app.engine.intent_handlers import (
    handle_start_over,
    handle_ambiguous,
    handle_context_switch,
    handle_question,
    handle_confirm_with_change,
    handle_session_summary,
    handle_paste_sql,
    handle_regenerate,
    handle_download_request,
    handle_explain,
)

from app.db.session_store import (
    save_session,
    load_session,
    delete_session as _delete_from_store,
)
from app.guardrails.input_gate import (
    assess_input,
    NON_TOPICAL,
    QUARANTINE_FLAG_THRESHOLD,
    record_quarantine,
)
from app.guardrails.output_gate import guard_text
from app.prompts import persona
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import User
from app.db.repositories.project_repo import ProjectRepository
from app.db.repositories.user_repo import UserRepository


logger = logging.getLogger(__name__)

# ── Triggers that mean the user wants to proceed to blueprint ────
GENERATE_BLUEPRINT_SIGNALS = [
    "generate blueprint", "generate schema", "generate now",
    "go ahead", "proceed", "enough", "ready", "let's go", "lets go",
    "next", "continue", "create schema", "build schema",
    "i'm ready", "im ready", "start generating", "generate",
    "yes generate", "yes, generate", "ok generate",
    "you understand", "you got it", "that's right proceed",
    "skip", "done answering",
]


async def create_session(db: Optional[AsyncSession] = None, user: Optional[User] = None, project_id: Optional[str] = None) -> ConversationState:
    """Create and persist a new conversation session, linking to PostgreSQL if authenticated."""
    session_id = str(uuid.uuid4())
    state = ConversationState(session_id=session_id)

    if db and user:
        project_repo = ProjectRepository(db)
        project = None
        
        if project_id:
            from app.core.auth_helpers import get_project_active_or_404
            project = await get_project_active_or_404(db, project_id)
        else:
            # If no project_id provided, create a new container
            project = await project_repo.create(
                owner_id=user.id,
                name="New Database Design",
                description="Database schema designed by SchemaAI",
            )

        # Create Version 1 (draft)
        version = await project_repo.create_version(
            project_id=project.id,
            requirement_text="Not yet described",
            created_by=user.id
        )

        state.project_id = project.id
        state.version_id = version.id

        # Warm-start from durable memory if this project has been worked on
        # before (Phase 3 — returning user, new session).
        if project_id:
            from app.services import conversation_memory
            await conversation_memory.rehydrate(state, db, project.id)

        # Link conversation session in PostgreSQL
        await project_repo.get_or_create_conversation(
            redis_session_id=session_id,
            project_id=project.id,
            version_id=version.id,
            user_id=user.id
        )
        await db.commit()

    save_session(state)
    logger.info(f"✅ Session created: {session_id} | project: {state.project_id} | version: {state.version_id}")
    return state



def get_session(session_id: str) -> Optional[ConversationState]:
    """Load session from Redis (or memory fallback)."""
    return load_session(session_id)

def delete_session(session_id: str):
    """Delete session from store."""
    _delete_from_store(session_id)
    logger.info(f"🗑️ Session deleted: {session_id}")


async def process_message(session_id: str, user_message: str, db: Optional[AsyncSession] = None, user: Optional[User] = None) -> dict:
    """
    Main conversation router.
    Detects intent first, then routes to the correct handler.
    Persists history to PostgreSQL if authenticated.
    """
    state = get_session(session_id)
    if not state:
        raise ValueError(f"Session '{session_id}' not found")

    if db and state.project_id:
        from app.core.auth_helpers import get_project_active_or_404
        try:
            await get_project_active_or_404(db, state.project_id)
        except Exception as e:
            delete_session(session_id)
            raise e

    # ── Input gate: classify before the engine sees it ─────────
    assessment = assess_input(user_message, state)
    mem_text = assessment.sanitized_for_memory or user_message

    # Record user message in Redis (storage-safe form)
    state.add_message("user", mem_text)

    # Persist user message to PostgreSQL
    db_conv = None
    if db and user:
        project_repo = ProjectRepository(db)
        # Ensure we have active links in Redis state
        if not state.project_id or not state.version_id:
            # Check if there is an active conversation in PostgreSQL
            db_conv = await project_repo.get_or_create_conversation(
                redis_session_id=session_id,
                user_id=user.id
            )
            state.project_id = db_conv.project_id
            state.version_id = db_conv.version_id
        else:
            db_conv = await project_repo.get_or_create_conversation(
                redis_session_id=session_id,
                project_id=state.project_id,
                version_id=state.version_id,
                user_id=user.id
            )
        
        # Save user message to PostgreSQL
        await project_repo.add_message(
            conversation_id=db_conv.id,
            role="user",
            content=mem_text,
            metadata={"stage": str(state.stage)}
        )

    # ── Finalizer: output-guard, record assistant turn, persist ─
    async def _finalize(response: dict) -> dict:
        raw_msg = response.get("message", "")
        safe_msg = guard_text(raw_msg, state)
        if safe_msg != raw_msg:
            response = {**response, "message": safe_msg}

        state.add_message("assistant", response.get("message", ""))

        if db and db_conv and state.version_id:
            _repo = ProjectRepository(db)
            # If we just created the blueprint, serialize and save to ProjectVersion
            if state.stage == ConversationStage.BLUEPRINT and state.blueprint:
                version = await _repo.get_version_by_id(state.version_id)
                if version:
                    bp_dict = state.blueprint.__dict__ if hasattr(state.blueprint, "__dict__") else state.blueprint
                    version.blueprint = bp_dict
                    if not version.requirement_text or version.requirement_text == "Not yet described":
                        version.requirement_text = state.requirement_summary
            await _repo.add_message(
                conversation_id=db_conv.id,
                role="assistant",
                content=response.get("message", ""),
                metadata={"stage": str(state.stage)},
            )

            # Phase 3 checkpoint — the user just confirmed the blueprint
            if state.stage == ConversationStage.CONFIRMED and state.project_id:
                from app.services import conversation_memory
                await conversation_memory.persist_checkpoint(
                    state, db, reason="blueprint_confirmed", commit=False
                )

            await db.commit()

        save_session(state)
        return response

    # ── Non-topical input: redirect in-persona, skip the engine ─
    if assessment.category in NON_TOPICAL:
        if assessment.quarantine:
            count = record_quarantine(session_id)
            if count >= QUARANTINE_FLAG_THRESHOLD:
                logger.warning(
                    f"🚩 Session flagged for review: {session_id} — "
                    f"{count} quarantined inputs (latest: {assessment.category})"
                )
        else:
            logger.info(
                f"↩️ Non-topical input redirected ({assessment.category}) — session {session_id}"
            )
        return await _finalize({
            "message": persona.fallback(assessment.reply_key),
            "stage": state.stage,
            "session_id": state.session_id,
        })

    # ── Observe → Think → Act (the lean turn loop) ─────────────
    from app.conversation.turn_loop import run_turn
    response = await run_turn(state, user_message, assessment)

    # ── Verify + record assistant turn + persist ──────────────
    return await _finalize(response)



# ── Stage handlers ───────────────────────────────────────────────


def _compile_pipeline_blueprint(
    state: ConversationState,
    requirement: str,
    domain: str,
    gst_required: bool,
    scale: str
) -> ProjectBlueprint:
    """Helper to run the L1-L8 compilation pipeline and save L1-L7 metadata in state."""
    from app.engine.abstraction_pipeline import (
        generate_l1_understanding,
        compile_l1_to_l2,
        compile_l2_to_l3,
        compile_l3_to_l4,
        compile_l4_to_l5_l6_l7,
        compile_to_l8_blueprint,
    )
    from app.services.rule_service import DOMAIN_MANDATORY_RULES

    # Run the 5-stage AI compilation
    l1 = generate_l1_understanding(requirement)
    l2 = compile_l1_to_l2(l1)
    l3 = compile_l2_to_l3(l1, l2)
    l4 = compile_l3_to_l4(l1, l3)
    l5, l6, l7 = compile_l4_to_l5_l6_l7(l1, l4)
    bp_spec = compile_to_l8_blueprint(l1, l4, l5, l6, l7)

    # Save L1-L7 data to state for downstream engines (Simulation, Council, etc.)
    state.l1_data = l1.model_dump()
    state.l2_data = l2.model_dump()
    state.l3_data = l3.model_dump()
    state.l4_data = l4.model_dump()
    state.l5_data = l5.model_dump()
    state.l6_data = l6.model_dump()
    state.l7_data = l7.model_dump()

    # Convert BlueprintSpec to ProjectBlueprint
    modules_list = []
    for m in bp_spec.modules:
        tables_list = []
        for t in m.tables:
            tables_list.append({
                "name": t.name,
                "purpose": t.purpose,
                "table_type": t.table_type,
                "entity_name": t.entity_name,
                "requires_archive": t.requires_archive,
                "requires_lifecycle": t.requires_lifecycle,
            })
        modules_list.append({
            "name": m.name,
            "description": m.description,
            "tables": tables_list,
            "dependencies": m.dependencies,
        })

    return ProjectBlueprint(
        project_name=bp_spec.project_name,
        description=bp_spec.description,
        domain=bp_spec.domain or domain,
        all_domains=[bp_spec.domain or domain],
        modules=modules_list,
        rules_to_apply=DOMAIN_MANDATORY_RULES.get(bp_spec.domain or domain, []),
        scale=bp_spec.scale or scale,
        gst_required=bp_spec.gst_required or gst_required,
    )


def _handle_blueprint_confirmation(state: ConversationState, user_message: str) -> dict:
    """User confirms or edits the blueprint."""
    user_lower = user_message.lower().strip()

    # User confirmed
    if user_lower in ["yes", "yes.", "confirm", "ok", "okay", "looks good", "correct",
                      "perfect", "great", "approved", "approve", "proceed", "go ahead"]:
        state.blueprint.confirmed = True
        state.stage = ConversationStage.CONFIRMED

        module_names = [m["name"] for m in state.blueprint.modules]
        tables_count = sum(len(m["tables"]) for m in state.blueprint.modules)

        confirm_message = f"""✅ **Blueprint confirmed!**
 
I will now generate:
- **{tables_count} tables** across **{len(module_names)} modules**
- Modules: {', '.join(module_names)}
- GST compliance: {'✓ Yes' if state.blueprint.gst_required else '✗ Not required'}
- Scale: {state.blueprint.scale.title()}
 
**Starting schema generation now...**"""

        state.add_message("assistant", confirm_message)
        return _handle_generation(state)

    # User wants to edit — recompile the blueprint as an async job
    elif user_lower.startswith(("edit", "add", "remove", "change", "update")):
        state.requirement_summary += f"\n\nUser edit request: {user_message}"
        return _blueprint_job_trigger(state)

    else:
        return {
            "message": (
                "Please type **YES** to confirm the blueprint, or describe what you'd like to change.\n\n"
                "For example:\n"
                "- *\"YES\"* → confirm and generate\n"
                "- *\"Add a notifications module\"* → update the blueprint\n"
                "- *\"Remove the analytics module\"* → simplify the blueprint"
            ),
            "stage": state.stage,
            "session_id": state.session_id,
        }


def _blueprint_job_trigger(state: ConversationState) -> dict:
    """
    Signal the frontend to run the L1-L8 blueprint compile as an async job
    (Phase 2 — this used to block the /message request for 30-60s). The
    frontend submits POST /planner/generate with mode="blueprint" and polls;
    the job writes the finished blueprint back into the session.
    """
    state.stage = ConversationStage.COMPILING
    save_session(state)
    return {
        "session_id": state.session_id,
        "stage": state.stage,
        "message": "Designing your blueprint — this'll just take a moment.",
        "requirement": state.requirement_summary,
        "mode": "blueprint",
    }


def _handle_generation(state: ConversationState) -> dict:
    """Set stage to GENERATING and return signal to start async generation on frontend."""
    state.stage = ConversationStage.GENERATING
    save_session(state)

    generation_requirement = _blueprint_to_requirement(state.blueprint)

    return {
        "session_id": state.session_id,
        "stage": state.stage,
        "message": "Starting on your schema now — I'll have it here shortly.",
        "requirement": generation_requirement,
        "blueprint": _blueprint_to_dict(state.blueprint) if state.blueprint else None,
    }


def _fallback_blueprint(requirement: str, domain: str) -> dict:
    """Fallback if AI returns invalid JSON."""
    return {
        "project_name": "Custom Project",
        "description": requirement[:100],
        "domain": domain,
        "gst_required": "gst" in requirement.lower() or "invoice" in requirement.lower(),
        "scale": "medium",
        "modules": [
            {
                "name": "Core",
                "description": "Core entities",
                "tables": [
                    {"name": "unique_id_header_all", "purpose": "Centralised ID registry"},
                ]
            }
        ]
    }


# ── Formatting helpers ───────────────────────────────────────────

def _format_questions(questions: list[dict]) -> str:
    """Format AI-generated questions into a numbered, readable list."""
    lines = []
    for q in questions:
        num = q.get("id", len(lines) + 1)
        question = q.get("question", "")
        lines.append(f"**{num}.** {question}")
    return "\n\n".join(lines)


def _confidence_bar(confidence: int) -> str:
    """Render a visual confidence bar showing how well the AI understands the project."""
    filled = round(confidence / 10)
    bar = "█" * filled + "░" * (10 - filled)
    label = (
        "Just getting started" if confidence < 40
        else "Building up" if confidence < 60
        else "Getting there" if confidence < 75
        else "Almost ready" if confidence < 85
        else "Ready to design!"
    )
    return f"\n**Understanding:** `{bar}` {confidence}% — _{label}_"


def _format_blueprint(blueprint: ProjectBlueprint) -> str:
    lines = []
    lines.append(f"### 📦 {blueprint.project_name}")
    lines.append(f"*{blueprint.description}*")
    lines.append("")
    lines.append(f"**Domain:** {blueprint.domain.replace('_', ' ').title()}")
    lines.append(f"**Scale:** {blueprint.scale.title()}")
    lines.append(f"**GST Compliance:** {'✓ Yes' if blueprint.gst_required else '✗ No'}")
    lines.append("")
    lines.append("**Modules & Tables:**")
    lines.append("")

    total_tables = 0
    for i, module in enumerate(blueprint.modules, 1):
        lines.append(f"**{i}. {module['name']}** — {module['description']}")
        for table in module.get("tables", []):
            lines.append(f"   • `{table['name']}` — {table['purpose']}")
            total_tables += 1
        lines.append("")

    lines.append(f"**Total tables to generate: {total_tables}**")
    return "\n".join(lines)


def _blueprint_to_dict(blueprint: ProjectBlueprint) -> dict:
    return {
        "project_name": blueprint.project_name,
        "description": blueprint.description,
        "domain": blueprint.domain,
        "all_domains": blueprint.all_domains,
        "scale": blueprint.scale,
        "gst_required": blueprint.gst_required,
        "modules": blueprint.modules,
        "rules_to_apply": blueprint.rules_to_apply,
        "confirmed": blueprint.confirmed,
    }


def _blueprint_to_requirement(blueprint: ProjectBlueprint) -> str:
    """Convert confirmed blueprint into a detailed requirement string for generation."""
    module_details = []
    for module in blueprint.modules:
        tables = ", ".join(t["name"] for t in module.get("tables", []))
        module_details.append(
            f"Module '{module['name']}': {module['description']} "
            f"(tables: {tables})"
        )

    return f"""Project: {blueprint.project_name}
Description: {blueprint.description}
Domain: {blueprint.domain}
Scale: {blueprint.scale}
GST Required: {blueprint.gst_required}

Modules to generate:
{chr(10).join(f'- {m}' for m in module_details)}

Generate ALL tables listed above. Follow all provided rules strictly."""


def _build_fix_requirement(original: str, validation, attempt: int) -> str:
    """Build a fix prompt that highlights specific issues."""
    if not validation or not validation.issues:
        return original

    issue_lines = []
    for issue in validation.issues:
        if issue.severity in ["critical", "high"]:
            issue_lines.append(
                f"- [{issue.severity.upper()}] {issue.issue} → {issue.suggestion}"
            )

    issues_text = "\n".join(issue_lines)

    return f"""{original}

IMPORTANT — Fix attempt {attempt + 1}:
The previous generation had these issues that MUST be fixed:

{issues_text}

Fix ALL issues above. Previous score was {validation.score}/100.
Target score: 85+/100."""