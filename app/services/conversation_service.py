# app/services/conversation_service.py

import uuid
import json
import logging
from typing import Optional
from app.engine.conversation_engine import (
    ConversationState,
    ConversationStage,
    ProjectBlueprint,
    get_clarifying_questions,
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

logger = logging.getLogger(__name__)

# In-memory session store
# Replace with Redis when you go to production
_sessions: dict[str, ConversationState] = {}


def create_session() -> ConversationState:
    """Create a new conversation session."""
    session_id = str(uuid.uuid4())
    state = ConversationState(session_id=session_id)
    _sessions[session_id] = state
    logger.info(f"✅ New session created: {session_id}")
    return state


def get_session(session_id: str) -> Optional[ConversationState]:
    return _sessions.get(session_id)


def delete_session(session_id: str):
    _sessions.pop(session_id, None)

def detect_intent(message: str, state: ConversationState) -> str:
    """
    Detect what the user actually wants to do.
    Returns intent string.
    """
    msg = message.lower().strip()

    # Start over signals
    if any(w in msg for w in ["start over", "reset", "new project", 
                                "forget it", "start fresh", "start again"]):
        return "start_over"

    # Confirmation with additions
    if msg.startswith("yes") and len(msg) > 10:
        return "confirm_with_changes"

    # Pure confirmation
    if msg in ["yes", "yes.", "confirm", "ok", "okay", 
               "looks good", "correct", "proceed", "go ahead"]:
        return "confirm"

    # Edit/modify signals
    if any(w in msg for w in ["edit", "change", "modify", "update", 
                                "remove", "delete", "replace"]):
        return "edit"

    # Add signals
    if any(w in msg for w in ["add", "include", "also need", 
                                "dont forget", "missing"]):
        return "add"

    # Regenerate signals
    if any(w in msg for w in ["regenerate", "redo", "again", 
                                "try again", "different"]):
        return "regenerate"

    # Question signals
    if msg.endswith("?") or msg.startswith(("what", "how", "why", 
                                              "when", "where", "who")):
        return "question"

    # Existing SQL paste
    if "create table" in msg.lower():
        return "paste_sql"

    # Context switch — completely different topic
    current_domain_words = []
    if state.blueprint:
        current_domain_words = [state.blueprint.domain]
    # If new domain keywords don't overlap with current
    # (complex — use AI for this in production)
    
    return "normal"

# app/services/conversation_service.py
# In process_message, update the routing:

def process_message(session_id: str, user_message: str) -> dict:
    """
    Main conversation router.
    Detects intent first, then routes to the correct handler.
    """
    state = get_session(session_id)
    if not state:
        raise ValueError(f"Session '{session_id}' not found")

    # Record user message
    state.add_message("user", user_message)

    # ── Step 1: Detect intent ────────────────────────────────────
    intent = detect_intent(user_message, state)
    logger.info(
        f"🎯 Intent: {intent.type} "
        f"(confidence: {intent.confidence}) "
        f"| Stage: {state.stage}"
    )

    # ── Step 2: Handle cross-stage intents first ─────────────────
    # These work regardless of what stage we're in

    if intent.type == IntentType.START_OVER:
        response = handle_start_over(state)

    elif intent.type == IntentType.AMBIGUOUS:
        response = handle_ambiguous(state, user_message, intent)

    elif intent.type == IntentType.CONTEXT_SWITCH:
        response = handle_context_switch(state, user_message)

    elif intent.type == IntentType.PASTE_SQL:
        response = handle_paste_sql(state, user_message)

    elif intent.type == IntentType.QUESTION:
        response = handle_question(state, user_message, intent)

    elif intent.type == IntentType.EXPLAIN:
        response = handle_explain(state, user_message, intent)

    elif intent.type == IntentType.DOWNLOAD:
        response = handle_download_request(state, intent)

    elif intent.type == IntentType.REGENERATE:
        response = handle_regenerate(state)
        # If blueprint confirmed, auto-trigger generation
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
        # Route within blueprint based on intent
        if intent.type == IntentType.CONFIRM:
            response = _handle_blueprint_confirmation(state, "yes")

        elif intent.type == IntentType.CONFIRM_WITH_CHANGE:
            response = handle_confirm_with_change(state, user_message, intent)

        elif intent.type in [IntentType.EDIT, IntentType.ADD, IntentType.REMOVE]:
            # Treat as edit instruction
            state.requirement_summary += f"\n\nUser modification: {user_message}"
            response = _handle_blueprint_confirmation(state, user_message)

        else:
            response = _handle_blueprint_confirmation(state, user_message)

    elif state.stage == ConversationStage.CONFIRMED:
        # Trigger generation
        response = _handle_generation(state)

    elif state.stage == ConversationStage.COMPLETE:
        # Handle post-generation requests
        if intent.type == IntentType.CONFIRM:
            response = handle_download_request(state, intent)
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

    # ── Step 5: Record assistant response ────────────────────────
    state.add_message("assistant", response.get("message", ""))
    return response

def _handle_initial(state: ConversationState, user_message: str) -> dict:
    """
    First message from user — detect domain, ask clarifying questions.
    """
    # Detect domain
    primary_domain, confidence = detect_domain(user_message)
    all_domains = detect_all_domains(user_message)

    state.requirement_summary = user_message

    # Get clarifying questions
    questions = get_clarifying_questions(primary_domain, user_message)

    # Move to clarifying stage
    state.stage = ConversationStage.CLARIFYING

    questions_text = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))

    message = f"""I understand you want to build a **{primary_domain.replace('_', ' ').title()}** system.

Before I generate your schema, I need a few quick details to make it production-ready:

{questions_text}

Please answer whichever are relevant — you can skip any that don't apply."""

    return {
        "message": message,
        "stage": state.stage,
        "detected_domain": primary_domain,
        "all_domains": all_domains,
        "session_id": state.session_id,
    }


def _handle_clarifying(state: ConversationState, user_message: str) -> dict:
    """
    User answered clarifying questions.
    Build blueprint and show for confirmation.
    """
    # Combine original requirement + clarifications
    full_requirement = f"""
Original requirement: {state.requirement_summary}

User clarifications: {user_message}
"""
    state.requirement_summary = full_requirement
    state.clarifications_done += 1

    # Generate blueprint using AI
    blueprint_response = _generate_blueprint(state, full_requirement)

    # Store blueprint in state
    state.blueprint = blueprint_response["blueprint"]
    state.stage = ConversationStage.BLUEPRINT

    # Format blueprint for display
    blueprint_text = _format_blueprint(state.blueprint)

    message = f"""Based on your requirements, here is your **Database Blueprint**:

{blueprint_text}

---
**Does this look correct?**

- Type **YES** to confirm and start generating the schema
- Type **EDIT** followed by what you want to change
- Type **ADD [module name]** to add a missing module

Example: *"EDIT — also add an attendance tracking module"*"""

    return {
        "message": message,
        "stage": state.stage,
        "blueprint": _blueprint_to_dict(state.blueprint),
        "session_id": state.session_id,
    }


def _handle_blueprint_confirmation(state: ConversationState, user_message: str) -> dict:
    """
    User confirms or edits the blueprint.
    """
    user_lower = user_message.lower().strip()

    # User confirmed
    if user_lower in ["yes", "yes.", "confirm", "ok", "okay", "looks good", "correct"]:
        state.blueprint.confirmed = True
        state.stage = ConversationStage.CONFIRMED

        module_names = [m["name"] for m in state.blueprint.modules]
        tables_count = sum(len(m["tables"]) for m in state.blueprint.modules)

        # ── Notify user ──────────────────────────────────────────
        confirm_message = f"""✅ **Blueprint confirmed!**

I will now generate:
- **{tables_count} tables** across **{len(module_names)} modules**
- Modules: {', '.join(module_names)}
- Rules to apply: {len(state.blueprint.rules_to_apply)} production rules
- GST compliance: {'✓ Yes' if state.blueprint.gst_required else '✗ Not required'}

**Starting schema generation now...**"""

        state.add_message("assistant", confirm_message)
        generation_response = _handle_generation(state)
        return generation_response

    # User wants to edit
    elif user_lower.startswith("edit") or user_lower.startswith("add") or user_lower.startswith("remove"):
        # Update requirement with edit instruction
        state.requirement_summary += f"\n\nUser edit request: {user_message}"

        # Regenerate blueprint
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
            "message": "Please type **YES** to confirm the blueprint, or **EDIT** followed by what you'd like to change.",
            "stage": state.stage,
            "session_id": state.session_id,
        }


def _handle_generation(state: ConversationState) -> dict:
    """
    Generate schema, validate, auto-fix if needed.
    """
    state.stage = ConversationStage.GENERATING
    validator = SchemaValidator()
    MAX_FIX_ATTEMPTS = 3

    # Build full requirement from blueprint
    generation_requirement = _blueprint_to_requirement(state.blueprint)

    best_schema = None
    best_score = 0
    best_validation = None

    for attempt in range(MAX_FIX_ATTEMPTS):
        logger.info(f"🔄 Generation attempt {attempt + 1}/{MAX_FIX_ATTEMPTS}")

        if attempt == 0:
            # First attempt — normal generation
            result = generate_database_schema(
                requirement=generation_requirement,
            )
        else:
            # Fix attempt — inject previous issues into requirement
            fix_requirement = _build_fix_requirement(
                generation_requirement,
                best_validation,
                attempt,
            )
            result = generate_database_schema(
                requirement=fix_requirement,
            )

        schema = result["schema"]
        validation = validator.validate(schema)
        state.fix_attempts = attempt + 1

        logger.info(f"  Score: {validation.score}/100 | Issues: {validation.total_issues}")

        if validation.score > best_score:
            best_score = validation.score
            best_schema = schema
            best_validation = validation

        # Stop if score is good enough
        if validation.score >= 80:
            logger.info(f"✅ Score {validation.score} >= 80 — stopping fix loop")
            break

    # Store final result
    state.schema = best_schema
    project_name = state.blueprint.project_name if state.blueprint else "project"

    grade = (
        "A" if best_score >= 90 else
        "B" if best_score >= 80 else
        "C" if best_score >= 70 else "D"
    )

    sql_path = generate_sql_file(
        schema_sql=best_schema,
        project_name=project_name,
        session_id=state.session_id,
    )

    pdf_path = generate_pdf_documentation(
        schema_sql=best_schema,
        project_name=project_name,
        session_id=state.session_id,
        blueprint=_blueprint_to_dict(state.blueprint) if state.blueprint else {},
        validation={
            "score": best_score,
            "grade": grade,
            "tables_found": best_validation.tables_found if best_validation else [],
            "total_issues": best_validation.total_issues if best_validation else 0,
            "issues": [
                {"rule_id": i.rule_id, "severity": i.severity,
                 "issue": i.issue, "suggestion": i.suggestion}
                for i in (best_validation.issues if best_validation else [])
            ],
        },
        metadata=result["metadata"],
        rules_applied=result["metadata"]["rules_applied"],
    )

    state.sql_file_path = sql_path
    state.pdf_file_path = pdf_path
    logger.info(f"📄 SQL: {sql_path}")
    logger.info(f"📋 PDF: {pdf_path}")
    state.validation_score = best_score
    state.stage = ConversationStage.COMPLETE

    # Build response message

    issues_text = ""
    if best_validation and best_validation.issues:
        issues_text = "\n\n**Minor notes:**\n" + "\n".join(
            f"- {i.issue}" for i in best_validation.issues[:3]
        )

    message = f"""✅ **Schema Generated Successfully!**

**Quality Score: {best_score}/100 — Grade {grade}**
**Tables Generated: {len(best_validation.tables_found if best_validation else [])}**
**Rules Applied: {result['metadata']['total_rules_applied']}**
**Fix Attempts: {state.fix_attempts}**
{issues_text}

Your files are ready:
📄 **schema.sql** — Run this directly in MySQL
📋 **documentation.pdf** — Complete logic guide for developers

What would you like to do?
- Download your files
- Ask me to explain any table
- Start a new schema"""

    return {
        "message": message,
        "stage": state.stage,
        "session_id": state.session_id,
        "schema": best_schema,
        "validation": {
            "score": best_score,
            "grade": grade,
            "tables_found": best_validation.tables_found if best_validation else [],
            "total_issues": best_validation.total_issues if best_validation else 0,
            "issues": [
                {
                    "rule_id": i.rule_id,
                    "severity": i.severity,
                    "issue": i.issue,
                    "suggestion": i.suggestion,
                }
                for i in (best_validation.issues if best_validation else [])
            ],
        },
        "metadata": result["metadata"],
    }


# ── Blueprint generation using AI ───────────────────────────────

def _generate_blueprint(state: ConversationState, requirement: str) -> dict:
    """Use AI to generate a structured blueprint from requirement."""

    from app.engine.rule_matcher import detect_domain, detect_all_domains
    from app.services.rule_service import DOMAIN_MANDATORY_RULES

    primary_domain, _ = detect_domain(requirement)
    all_domains = detect_all_domains(requirement)
    mandatory_rules = DOMAIN_MANDATORY_RULES.get(primary_domain, [])

    system_prompt = """You are a database architect. 
Analyse the requirement and return a JSON blueprint ONLY.
No explanation, no markdown, just valid JSON.

Return exactly this structure:
{
  "project_name": "short project name",
  "description": "one sentence description",
  "domain": "primary domain",
  "gst_required": true or false,
  "scale": "small" or "medium" or "large",
  "modules": [
    {
      "name": "Module Name",
      "description": "what this module does",
      "tables": [
        {"name": "table_name_header_all", "purpose": "what this table stores"}
      ]
    }
  ]
}

Rules:
- Table names must end with _header_all, _transaction_all, _configuration_all, _archive_all, or _life_cycle_all
- Always include unique_id_header_all in the first module
- Group related tables into logical modules
- 3-8 modules maximum
- 2-6 tables per module"""

    user_prompt = f"Requirement: {requirement}"

    response = generate_schema(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    # Parse JSON from response
    content = response["content"].strip()
    # Strip markdown code blocks if present
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    content = content.strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Fallback blueprint if AI returns invalid JSON
        data = _fallback_blueprint(requirement, primary_domain)

    blueprint = ProjectBlueprint(
        project_name=data.get("project_name", "My Project"),
        description=data.get("description", requirement[:100]),
        domain=primary_domain,
        all_domains=all_domains,
        modules=data.get("modules", []),
        rules_to_apply=mandatory_rules,
        scale=data.get("scale", "medium"),
        gst_required=data.get("gst_required", False),
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

def _format_blueprint(blueprint: ProjectBlueprint) -> str:
    lines = []
    lines.append(f"### 📦 {blueprint.project_name}")
    lines.append(f"*{blueprint.description}*")
    lines.append(f"")
    lines.append(f"**Domain:** {blueprint.domain.replace('_', ' ').title()}")
    lines.append(f"**Scale:** {blueprint.scale.title()}")
    lines.append(f"**GST Compliance:** {'✓ Yes' if blueprint.gst_required else '✗ No'}")
    lines.append(f"")
    lines.append(f"**Modules & Tables:**")
    lines.append(f"")

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


def _build_fix_requirement(
    original: str,
    validation,
    attempt: int,
) -> str:
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