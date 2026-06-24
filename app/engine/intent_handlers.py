# app/engine/intent_handlers.py

import logging
from app.engine.conversation_engine import ConversationState, ConversationStage
from app.engine.intent_detector import Intent, IntentType

logger = logging.getLogger(__name__)


def handle_start_over(state: ConversationState) -> dict:
    """
    User wants to completely restart.
    Archive current progress, reset state.
    """
    old_project = ""
    if state.blueprint:
        old_project = f" (previous project: {state.blueprint.project_name})"

    # Reset everything except session_id
    state.stage = ConversationStage.INITIAL
    state.blueprint = None
    state.requirement_summary = ""
    state.clarifications_done = 0
    state.schema = None
    state.validation_score = None
    state.fix_attempts = 0
    state.sql_file_path = None
    state.pdf_file_path = None

    logger.info(f"🔄 Session reset{old_project}")

    return {
        "message": f"""✅ Starting fresh{old_project}.

Tell me about your new project — what kind of database do you need to build?
Describe what it does, who uses it, and what data it needs to manage.""",
        "stage": state.stage,
        "session_id": state.session_id,
        "action": "reset",
    }


def handle_ambiguous(
    state: ConversationState,
    user_message: str,
    intent: Intent,
) -> dict:
    """
    User sent a vague or very short message.
    Ask for clarification based on current stage.
    """
    stage = state.stage

    if stage == ConversationStage.INITIAL:
        prompt = "Could you tell me more about the project? What kind of system do you want to build?"

    elif stage == ConversationStage.CLARIFYING:
        prompt = (
            "I didn't quite catch that. Could you answer the questions above? "
            "You can skip any that don't apply."
        )

    elif stage == ConversationStage.BLUEPRINT:
        prompt = (
            "I'm not sure what you'd like to do with the blueprint. "
            "Please type:\n"
            "- **YES** to confirm and generate\n"
            "- **EDIT [what to change]** to modify it\n"
            "- **ADD [module name]** to add something\n"
            "- **REMOVE [module name]** to remove something"
        )

    elif stage == ConversationStage.COMPLETE:
        prompt = (
            "Your schema is ready. Would you like to:\n"
            "- **Download** your SQL and PDF files\n"
            "- **Explain** a specific table\n"
            "- **Start over** with a new project"
        )

    else:
        prompt = f"Could you be more specific? I received: *\"{user_message}\"*"

    return {
        "message": prompt,
        "stage": state.stage,
        "session_id": state.session_id,
        "action": "clarification_needed",
    }


def handle_context_switch(
    state: ConversationState,
    user_message: str,
) -> dict:
    """
    User seems to be switching to a different project entirely.
    Ask them what they want to do.
    """
    old_project = state.blueprint.project_name if state.blueprint else "your current project"

    return {
        "message": f"""I noticed this seems quite different from **{old_project}**.

What would you like to do?

1. **Add this to the current project** — I'll incorporate it as a new module
2. **Start a completely new project** — type *"start over"* and describe the new one
3. **Continue with the current blueprint** — type *"continue"* to go back

What's your preference?""",
        "stage": state.stage,
        "session_id": state.session_id,
        "action": "context_switch_detected",
        "detected_switch": user_message[:100],
    }


def handle_question(
    state: ConversationState,
    user_message: str,
    intent: Intent,
) -> dict:
    """
    User asked a question.
    Answer it using AI, then re-prompt based on current stage.
    """
    from app.services.ai_service import generate_schema

    # Build context-aware system prompt for answering questions
    context = ""
    if state.blueprint:
        context = f"The user is building: {state.blueprint.project_name} — {state.blueprint.description}"

    system_prompt = f"""You are a database design expert assistant.
Answer the user's question clearly and concisely in 2-4 sentences.
Use simple language — no jargon unless necessary.
{context}
After answering, end with one sentence bringing them back on track."""

    user_prompt = f"Question: {user_message}"

    try:
        response = generate_schema(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        answer = response["content"]
    except Exception as e:
        logger.error(f"Question answering failed: {e}")
        answer = f"Good question. {user_message} — Let me continue with your schema generation."

    # Add stage-specific follow-up
    follow_up = _get_stage_follow_up(state)

    return {
        "message": f"{answer}\n\n---\n{follow_up}",
        "stage": state.stage,
        "session_id": state.session_id,
        "action": "question_answered",
    }


def handle_confirm_with_change(
    state: ConversationState,
    user_message: str,
    intent: Intent,
) -> dict:
    """
    User said YES but also wants to make a change.
    Apply the change to blueprint, then auto-confirm.
    """
    change_content = intent.extracted_content or user_message

    logger.info(f"✅ Confirm with change: {change_content}")

    # Update requirement with the change
    state.requirement_summary += f"\n\nUser requested change after confirmation: {change_content}"

    # Regenerate blueprint with the change included
    from app.services.conversation_service import _generate_blueprint, _format_blueprint

    blueprint_response = _generate_blueprint(state, state.requirement_summary)
    state.blueprint = blueprint_response["blueprint"]

    blueprint_text = _format_blueprint(state.blueprint)

    return {
        "message": f"""I've applied your change and updated the blueprint:

{blueprint_text}

---
Type **YES** to confirm this updated version, or let me know if you need more changes.""",
        "stage": ConversationStage.BLUEPRINT,
        "session_id": state.session_id,
        "blueprint": _blueprint_to_dict_safe(state.blueprint),
        "action": "blueprint_updated",
    }


def handle_session_summary(state: ConversationState) -> dict:
    """
    Show summary of current session state.
    Used when user returns after a gap or seems confused.
    """
    stage_descriptions = {
        ConversationStage.INITIAL:     "Just started — waiting for your project description",
        ConversationStage.CLARIFYING:  "Gathering requirements — waiting for your answers",
        ConversationStage.BLUEPRINT:   "Blueprint ready — waiting for your confirmation",
        ConversationStage.CONFIRMED:   "Blueprint confirmed — about to generate schema",
        ConversationStage.GENERATING:  "Schema generation in progress",
        ConversationStage.COMPLETE:    "Schema generated and ready to download",
    }

    project_info = ""
    if state.blueprint:
        tables_count = sum(len(m.get("tables", [])) for m in state.blueprint.modules)
        project_info = f"""
**Project:** {state.blueprint.project_name}
**Tables planned:** {tables_count}
**Domain:** {state.blueprint.domain.replace('_', ' ').title()}
**GST:** {'Yes' if state.blueprint.gst_required else 'No'}"""

    score_info = ""
    if state.validation_score:
        score_info = f"\n**Schema Score:** {state.validation_score}/100"

    stage_desc = stage_descriptions.get(state.stage, "Unknown stage")

    return {
        "message": f"""Here's where we are:

**Status:** {stage_desc}
{project_info}{score_info}

**Messages exchanged:** {len(state.messages)}

What would you like to do?
- **Continue** from where we left off
- **Start over** with a new project
- **Download** your files (if schema is ready)""",
        "stage": state.stage,
        "session_id": state.session_id,
        "action": "session_summary",
    }


def handle_paste_sql(
    state: ConversationState,
    user_message: str,
) -> dict:
    """
    User pasted existing SQL.
    Parse it, tell them what we found, offer to build on top.
    """
    import re

    # Extract table names from pasted SQL
    tables = re.findall(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?',
        user_message, re.IGNORECASE
    )

    if not tables:
        return {
            "message": "I couldn't find any CREATE TABLE statements in what you pasted. Could you share the SQL again?",
            "stage": state.stage,
            "session_id": state.session_id,
            "action": "paste_sql_failed",
        }

    # Store the existing SQL in requirement
    state.requirement_summary += f"\n\nEXISTING TABLES (already built — do NOT regenerate these):\n"
    state.requirement_summary += "\n".join(f"- {t}" for t in tables)
    state.requirement_summary += f"\n\nOriginal SQL:\n{user_message}"

    tables_list = "\n".join(f"  • `{t}`" for t in tables)

    return {
        "message": f"""I found **{len(tables)} existing tables** in your SQL:

{tables_list}

I'll build on top of these — I won't regenerate any tables you already have.

What additional tables or modules do you need? 
Or tell me what's still missing from your database.""",
        "stage": ConversationStage.CLARIFYING,
        "session_id": state.session_id,
        "existing_tables": tables,
        "action": "existing_sql_parsed",
    }


def handle_regenerate(state: ConversationState) -> dict:
    """
    User wants to regenerate the schema.
    Reset generation state, keep blueprint.
    """
    if not state.blueprint or not state.blueprint.confirmed:
        return {
            "message": "There's no confirmed blueprint to regenerate from. "
                      "Please confirm a blueprint first.",
            "stage": state.stage,
            "session_id": state.session_id,
        }

    # Reset generation state only
    state.schema = None
    state.validation_score = None
    state.fix_attempts = 0
    state.sql_file_path = None
    state.pdf_file_path = None
    state.stage = ConversationStage.CONFIRMED

    return {
        "message": f"""🔄 Regenerating schema for **{state.blueprint.project_name}**...

I'll use the same blueprint but generate a fresh schema.
This sometimes produces better results on a second attempt.

Starting now — this will take 15-30 seconds.""",
        "stage": state.stage,
        "session_id": state.session_id,
        "action": "regenerating",
        "ready_to_generate": True,
    }


def handle_download_request(
    state: ConversationState,
    intent: Intent,
) -> dict:
    """
    User wants to download their files.
    """
    if state.stage != ConversationStage.COMPLETE:
        return {
            "message": "Your schema hasn't been generated yet. "
                      "Complete the conversation first to generate your files.",
            "stage": state.stage,
            "session_id": state.session_id,
        }

    file_type = intent.sub_action or "both"
    session_id = state.session_id

    links = []
    if file_type in ["sql", "both"] and state.sql_file_path:
        links.append(f"📄 **SQL File:** `http://localhost:8000/conversation/download/sql/{session_id}`")
    if file_type in ["pdf", "both"] and state.pdf_file_path:
        links.append(f"📋 **PDF Docs:** `http://localhost:8000/conversation/download/pdf/{session_id}`")

    if not links:
        return {
            "message": "Files are not ready yet. Please generate a schema first.",
            "stage": state.stage,
            "session_id": state.session_id,
        }

    return {
        "message": f"""Your files are ready to download:

{chr(10).join(links)}

Is there anything else you need?
- **Explain [table name]** — I'll explain what a specific table does
- **Start over** — Build a new schema
- **Regenerate** — Generate a fresh version of this schema""",
        "stage": state.stage,
        "session_id": state.session_id,
        "download_urls": {
            "sql": f"/conversation/download/sql/{session_id}" if state.sql_file_path else None,
            "pdf": f"/conversation/download/pdf/{session_id}" if state.pdf_file_path else None,
        },
        "action": "download_links_provided",
    }


def handle_explain(
    state: ConversationState,
    user_message: str,
    intent: Intent,
) -> dict:
    """
    User wants an explanation of a specific table or concept.
    """
    from app.services.ai_service import generate_schema
    from app.services.file_service import _get_table_logic

    target = intent.extracted_content or user_message

    # Check if it's a table name
    is_table = bool(re.search(
        r'\w+_(?:header|transaction|archive|life_cycle|configuration)_all',
        target, re.IGNORECASE
    ))

    import re

    if is_table and state.schema:
        table_name = re.search(
            r'(\w+_(?:header|transaction|archive|life_cycle|configuration)_all)',
            target, re.IGNORECASE
        )
        if table_name:
            tn = table_name.group(1)
            purpose, insert_when, update_when = _get_table_logic(
                tn, _blueprint_to_dict_safe(state.blueprint)
            )
            return {
                "message": f"""**`{tn}`**

**Purpose:** {purpose}

**INSERT when:** {insert_when}

**UPDATE when:** {update_when}

Need me to explain another table or anything else?""",
                "stage": state.stage,
                "session_id": state.session_id,
                "action": "table_explained",
            }

    # General explanation via AI
    context = ""
    if state.schema:
        context = f"\nContext — the current schema includes these tables:\n{state.schema[:500]}..."

    system_prompt = f"""You are a database design expert.
Explain the concept or table the user is asking about clearly.
Keep it under 100 words. Be practical and direct.{context}"""

    try:
        response = generate_schema(
            system_prompt=system_prompt,
            user_prompt=f"Explain: {target}",
        )
        explanation = response["content"]
    except Exception:
        explanation = f"**{target}** — I wasn't able to generate an explanation right now."

    follow_up = _get_stage_follow_up(state)

    return {
        "message": f"{explanation}\n\n---\n{follow_up}",
        "stage": state.stage,
        "session_id": state.session_id,
        "action": "explained",
    }


# ── Helpers ──────────────────────────────────────────────────────

def _get_stage_follow_up(state: ConversationState) -> str:
    """Get a stage-appropriate follow-up prompt."""
    if state.stage == ConversationStage.CLARIFYING:
        return "Now, could you answer the clarifying questions above so I can build your blueprint?"
    elif state.stage == ConversationStage.BLUEPRINT:
        return "Type **YES** to confirm the blueprint, or let me know what you'd like to change."
    elif state.stage == ConversationStage.COMPLETE:
        return "Type **download** to get your files, or **start over** for a new project."
    return "How can I help you continue?"


def _blueprint_to_dict_safe(blueprint) -> dict:
    """Safely convert blueprint to dict."""
    if blueprint is None:
        return {}
    return {
        "project_name": blueprint.project_name,
        "description": blueprint.description,
        "domain": blueprint.domain,
        "modules": blueprint.modules,
        "gst_required": blueprint.gst_required,
        "scale": blueprint.scale,
    }