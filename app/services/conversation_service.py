import uuid
import json
import logging
import re
from typing import Optional
from app.engine.conversation_engine import (
    ConversationState,
    ConversationStage,
    ProjectBlueprint,
    generate_dynamic_clarifying_questions,
    assess_understanding,
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

    # ── Step 1: Detect intent ────────────────────────────────────
    intent = detect_intent(user_message, state)
    logger.info(
        f"🎯 Intent: {intent.type} "
        f"(confidence: {intent.confidence}) "
        f"| Stage: {state.stage}"
    )

    # ── Step 2: Handle cross-stage intents first ─────────────────
    if intent.type == IntentType.START_OVER:
        response = handle_start_over(state)

    elif intent.type == IntentType.AMBIGUOUS and state.stage not in [
        ConversationStage.INITIAL, ConversationStage.CLARIFYING
    ]:
        response = handle_ambiguous(state, user_message, intent)

    elif intent.type == IntentType.CONTEXT_SWITCH:
        response = handle_context_switch(state, user_message)

    elif intent.type == IntentType.PASTE_SQL:
        response = handle_paste_sql(state, user_message)

    elif intent.type == IntentType.EXPLAIN:
        response = handle_explain(state, user_message, intent)

    elif intent.type == IntentType.DOWNLOAD:
        response = handle_download_request(state, intent)

    elif intent.type == IntentType.REGENERATE:
        response = handle_regenerate(state)
        if response.get("ready_to_generate"):
            response = _handle_generation(state)

    # ── Step 3: Handle "where am I?" confusion ───────────────────
    elif user_message.lower().strip() in [
        "status", "where are we", "what's happening",
        "whats happening", "progress", "summary"
    ]:
        response = handle_session_summary(state)

    # ── Step 4: Stage-specific routing ──────────────────────────
    elif state.stage == ConversationStage.INITIAL:
        response = _handle_initial(state, user_message)

    elif state.stage == ConversationStage.CLARIFYING:
        response = _handle_clarifying(state, user_message)

    elif state.stage == ConversationStage.BLUEPRINT:
        if intent.type == IntentType.CONFIRM:
            response = _handle_blueprint_confirmation(state, "yes")
        elif intent.type == IntentType.CONFIRM_WITH_CHANGE:
            response = handle_confirm_with_change(state, user_message, intent)
        elif intent.type in [IntentType.EDIT, IntentType.ADD, IntentType.REMOVE]:
            state.requirement_summary += f"\n\nUser modification: {user_message}"
            response = _handle_blueprint_confirmation(state, user_message)
        else:
            response = _handle_blueprint_confirmation(state, user_message)

    elif state.stage == ConversationStage.CONFIRMED:
        response = _handle_generation(state)

    elif state.stage == ConversationStage.GENERATING:
        # User sent a message while generation is in progress or after a timeout failure.
        # REGENERATE intent (continue/retry/resume) re-triggers generation.
        if intent.type == IntentType.REGENERATE:
            state.schema = None
            state.validation_score = None
            state.fix_attempts = 0
            state.sql_file_path = None
            state.pdf_file_path = None
            state.stage = ConversationStage.CONFIRMED
            response = _handle_generation(state)
        else:
            response = {
                "message": (
                    "⏳ Schema generation is still in progress — please wait.\n\n"
                    "If it seems stuck or timed out, type **continue generating** or **retry** "
                    "to start a fresh generation attempt."
                ),
                "stage": state.stage,
                "session_id": state.session_id,
            }

    elif state.stage == ConversationStage.COMPLETE:
        if intent.type == IntentType.CONFIRM:
            response = handle_download_request(state, intent)
        elif intent.type == IntentType.QUESTION:
            response = handle_question(state, user_message, intent)
        else:
            response = {
                "message": (
                    "✅ Your schema is complete!\n\n"
                    "- Type **download** to get your SQL and PDF files\n"
                    "- Type **explain [table name]** to understand a table\n"
                    "- Type **start over** to build a new schema"
                ),
                "stage": state.stage,
                "session_id": state.session_id,
            }

    else:
        response = {
            "message": "Something unexpected happened. Type **start over** to begin fresh.",
            "stage": state.stage,
            "session_id": state.session_id,
        }

    # ── Step 5: output-guard, record assistant turn, persist ────
    return await _finalize(response)



# ── Stage handlers ───────────────────────────────────────────────

def _handle_initial(state: ConversationState, user_message: str) -> dict:
    """
    First message from user — store it, detect domain, and start
    the dynamic AI-powered clarification loop. No static questions.
    """
    primary_domain, confidence = detect_domain(user_message)
    all_domains = detect_all_domains(user_message)

    # Store the initial requirement
    state.requirement_summary = user_message
    state.stage = ConversationStage.CLARIFYING

    logger.info(f"🚀 Starting dynamic clarification — domain: {primary_domain}, confidence: {confidence}")

    # Generate first round of AI-powered questions
    q_data = generate_dynamic_clarifying_questions(state, primary_domain)

    questions = q_data.get("questions", [])
    understood = q_data.get("understood_so_far", "")
    confidence_pct = q_data.get("confidence", 0)

    # Track asked questions
    state.questions_asked.extend([q["question"] for q in questions])
    state.understood_aspects = q_data.get("understood", {})

    questions_text = _format_questions(questions)
    domain_label = primary_domain.replace("_", " ").title()

    message = f"""Great! I can see this is a **{domain_label}** project. 🎯

Here's what I understand so far:
_{understood}_

To design the perfect database schema, I have a few questions:

{questions_text}

Feel free to answer all or just the ones relevant to your project.
When you're ready to generate the blueprint, just say **"Generate Blueprint"**."""

    return {
        "message": message,
        "stage": state.stage,
        "detected_domain": primary_domain,
        "all_domains": all_domains,
        "clarification_round": 1,
        "understanding_confidence": confidence_pct,
        "session_id": state.session_id,
    }


def _handle_clarifying(state: ConversationState, user_message: str) -> dict:
    """
    Dynamic multi-round clarification.
    
    Each round:
    1. Appends user's answer to the requirement summary
    2. Asks AI to assess current understanding (confidence 0-100%)
    3. If user explicitly says "generate" OR confidence >= 85%, proceed to blueprint
    4. Otherwise, generate next targeted set of questions and loop back
    """
    msg_lower = user_message.lower().strip()

    # ── Check if user wants to proceed ──────────────────────────
    wants_to_proceed = any(signal in msg_lower for signal in GENERATE_BLUEPRINT_SIGNALS)

    # Append the user's answer
    if user_message.strip():
        if state.clarifications_done == 0:
            # First clarification round
            state.requirement_summary += f"\n\nAdditional context:\n{user_message}"
        else:
            state.requirement_summary += f"\n\nRound {state.clarifications_done + 1} answers:\n{user_message}"

    state.clarifications_done += 1

    # Detect domain from accumulated requirement
    primary_domain, _ = detect_domain(state.requirement_summary)

    # ── Assess current understanding ─────────────────────────────
    understanding = assess_understanding(state, primary_domain)
    current_confidence = understanding.get("confidence", 50)
    logger.info(f"🧠 Understanding confidence after round {state.clarifications_done}: {current_confidence}%")

    # Update accumulated understanding
    state.understood_aspects = understanding.get("understood", {})

    # ── Decide: proceed or ask more questions ────────────────────
    # The transition to the blueprint stage must be EXPLICITLY triggered by the user
    # ("the bot should be asking more and more question... until the user verifies")
    if wants_to_proceed:
        logger.info(
            f"✅ Proceeding to blueprint generation — "
            f"confidence: {current_confidence}%, "
            f"user triggered: {wants_to_proceed}, "
            f"rounds done: {state.clarifications_done}"
        )
        return _generate_blueprint_from_understanding(state, primary_domain)

    # ── Generate next round of questions ─────────────────────────
    q_data = generate_dynamic_clarifying_questions(state, primary_domain)

    questions = q_data.get("questions", [])
    still_unclear = q_data.get("key_gaps", [])
    new_confidence = q_data.get("confidence", current_confidence)

    # Track questions
    state.questions_asked.extend([q["question"] for q in questions])

    questions_text = _format_questions(questions)
    one_line = understanding.get("one_line_summary", "your project")
    round_num = state.clarifications_done + 1

    # Build a friendly, context-aware response
    affirmations = [
        "Thanks, that's helpful! 👍",
        "Got it, that clarifies a lot!",
        "Perfect, I'm getting a clearer picture!",
        "Great context! 🙌",
        "That helps me understand the scope better!",
    ]
    affirmation = affirmations[(state.clarifications_done - 1) % len(affirmations)]

    confidence_bar = _confidence_bar(new_confidence)
    gaps_text = ""
    if still_unclear:
        gaps_text = f"\n\n*Still figuring out: {', '.join(still_unclear[:3])}*"

    # If confidence is very high, explicitly prompt that we are ready but wait for their go-ahead
    if new_confidence >= 85:
        ready_prompt = "\n\n💡 **I have a very clear understanding and am ready to design your blueprint!** Feel free to answer the questions above if you want to add more detail, or click **Generate Blueprint** to see the design."
    else:
        ready_prompt = "\n\nAnswer what you can — or click **Generate Blueprint** below when you feel I have understood your project well enough."

    message = f"""{affirmation}

**My understanding so far:** _{one_line}_
{confidence_bar}{gaps_text}

Here are my next questions (Round {round_num}):

{questions_text}{ready_prompt}"""

    return {
        "message": message,
        "stage": state.stage,
        "clarification_round": round_num,
        "understanding_confidence": new_confidence,
        "session_id": state.session_id,
    }


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


def _generate_blueprint_from_understanding(state: ConversationState, primary_domain: str) -> dict:
    """
    Generate the blueprint using all accumulated context.
    Called when either user triggers generation or AI confidence >= 85%.
    """
    full_requirement = state.requirement_summary

    gst_required = any(w in full_requirement.lower()
                       for w in ["gst", "invoice", "tax", "billing"])

    scale = "medium"
    req_lower = full_requirement.lower()
    if any(w in req_lower for w in ["large", "million", "millions", "enterprise", "thousands", "billion"]):
        scale = "large"
    elif any(w in req_lower for w in ["small", "startup", "simple", "basic"]):
        scale = "small"

    blueprint = _compile_pipeline_blueprint(
        state=state,
        requirement=full_requirement,
        domain=primary_domain,
        gst_required=gst_required,
        scale=scale,
    )

    state.blueprint = blueprint
    state.stage = ConversationStage.BLUEPRINT

    blueprint_text = _format_blueprint(state.blueprint)
    total_tables = sum(len(m.get("tables", [])) for m in state.blueprint.modules)

    message = f"""I believe I understand your project well now! Here's the **Database Blueprint** I've designed:
 
{blueprint_text}
 
**~{total_tables} tables** across **{len(state.blueprint.modules)} modules** — tailored specifically to your project.
 
---
**Does this match your vision?**
- Type **YES** to confirm and generate the full SQL schema
- Type **EDIT [what to change]** to adjust the blueprint
- Type **ADD [module name]** to include an additional module"""

    return {
        "message": message,
        "stage": state.stage,
        "blueprint": _blueprint_to_dict(state.blueprint),
        "session_id": state.session_id,
    }


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

    # User wants to edit
    elif user_lower.startswith(("edit", "add", "remove", "change", "update")):
        state.requirement_summary += f"\n\nUser edit request: {user_message}"
        blueprint_response = _generate_blueprint(state, state.requirement_summary)
        state.blueprint = blueprint_response["blueprint"]
        blueprint_text = _format_blueprint(state.blueprint)

        message = f"""I've updated the blueprint based on your feedback:
 
{blueprint_text}
 
---
Type **YES** to confirm, or let me know if you need more changes."""

        return {
            "message": message,
            "stage": state.stage,
            "blueprint": _blueprint_to_dict(state.blueprint),
            "session_id": state.session_id,
        }

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


def _handle_generation(state: ConversationState) -> dict:
    """Set stage to GENERATING and return signal to start async generation on frontend."""
    state.stage = ConversationStage.GENERATING
    save_session(state)

    generation_requirement = _blueprint_to_requirement(state.blueprint)

    return {
        "session_id": state.session_id,
        "stage": state.stage,
        "message": "🚀 Starting schema generation...",
        "requirement": generation_requirement,
        "blueprint": _blueprint_to_dict(state.blueprint) if state.blueprint else None,
    }


def _generate_blueprint(state: ConversationState, requirement: str) -> dict:
    """Use L1-L8 compilation pipeline to regenerate a structured blueprint from requirement."""
    from app.engine.rule_matcher import detect_domain
    primary_domain, _ = detect_domain(requirement)

    gst_required = any(w in requirement.lower()
                       for w in ["gst", "invoice", "tax", "billing"])

    scale = "medium"
    req_lower = requirement.lower()
    if any(w in req_lower for w in ["large", "million", "millions", "enterprise", "thousands", "billion"]):
        scale = "large"
    elif any(w in req_lower for w in ["small", "startup", "simple", "basic"]):
        scale = "small"

    blueprint = _compile_pipeline_blueprint(
        state=state,
        requirement=requirement,
        domain=primary_domain,
        gst_required=gst_required,
        scale=scale,
    )
    return {"blueprint": blueprint}


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