# app/engine/conversation_engine.py

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional
import json
import logging

logger = logging.getLogger(__name__)


class ConversationStage(str, Enum):
    INITIAL         = "initial"           # User just described project
    CLARIFYING      = "clarifying"        # AI dynamically asking questions round-by-round
    BLUEPRINT       = "blueprint"         # Showing table plan for confirmation
    CONFIRMED       = "confirmed"         # User confirmed — ready to generate
    GENERATING      = "generating"        # Schema being generated
    FIXING          = "fixing"            # Auto-fix loop running
    COMPLETE        = "complete"          # Done — files ready


@dataclass
class ConversationMessage:
    role: str           # "user" | "assistant"
    content: str
    stage: Optional[str] = None


@dataclass
class ProjectBlueprint:
    """The confirmed plan before generation starts."""
    project_name: str
    description: str
    domain: str
    all_domains: list[str]
    modules: list[dict]          # [{name, tables, description}]
    rules_to_apply: list[int]    # rule IDs
    scale: str                   # "small" | "medium" | "large"
    gst_required: bool
    confirmed: bool = False


@dataclass
class ConversationState:
    session_id: str
    stage: ConversationStage = ConversationStage.INITIAL
    messages: list[ConversationMessage] = field(default_factory=list)
    blueprint: Optional[ProjectBlueprint] = None
    requirement_summary: str = ""
    clarifications_done: int = 0
    questions_asked: list[str] = field(default_factory=list)   # track what was already asked
    understood_aspects: dict = field(default_factory=dict)      # accumulated understanding
    schema: Optional[str] = None
    validation_score: Optional[int] = None
    fix_attempts: int = 0
    project_id: Optional[str] = None
    version_id: Optional[str] = None
    sql_file_path: Optional[str] = None
    pdf_file_path: Optional[str] = None

    # L1-L7 Abstraction Pipeline Metadata
    l1_data: Optional[dict] = None
    l2_data: Optional[dict] = None
    l3_data: Optional[dict] = None
    l4_data: Optional[dict] = None
    l5_data: Optional[dict] = None
    l6_data: Optional[dict] = None
    l7_data: Optional[dict] = None

    def add_message(self, role: str, content: str):
        self.messages.append(
            ConversationMessage(role=role, content=content, stage=self.stage)
        )

    def get_history_for_ai(self) -> list[dict]:
        """Return message history in AI format."""
        return [
            {"role": m.role, "content": m.content}
            for m in self.messages
        ]

    def get_conversation_so_far(self) -> str:
        """Return a readable transcript of the conversation."""
        lines = []
        for m in self.messages[-20:]:  # last 20 messages for context
            prefix = "User" if m.role == "user" else "Assistant"
            lines.append(f"{prefix}: {m.content[:400]}")
        return "\n".join(lines)


# ── Dynamic AI-powered clarifying question generator ────────────

def generate_dynamic_clarifying_questions(
    state: ConversationState,
    domain: str,
) -> dict:
    """
    Use the LLM to dynamically generate the next batch of clarifying questions
    based on what has been discussed so far and what gaps remain.

    Returns a dict:
    {
        "questions": ["q1", "q2", ...],
        "understood_so_far": "summary of what AI knows",
        "confidence": 0-100,  # how well the AI understands the project
        "ready_for_blueprint": bool
    }
    """
    from app.services.ai_service import generate_schema

    conversation_transcript = state.get_conversation_so_far()
    asked_questions = "\n".join(f"- {q}" for q in state.questions_asked) if state.questions_asked else "None yet"
    round_number = state.clarifications_done + 1

    system_prompt = """You are a senior database architect having a discovery conversation with a client.
Your goal is to understand their project deeply enough to design a perfect, production-ready database schema.

You must:
1. Analyze what you already know from the conversation
2. Identify the MOST IMPORTANT remaining gaps in your understanding
3. Ask 3-4 targeted, specific questions that will maximally improve your understanding
4. Each question must dig deeper than surface-level — ask about edge cases, business rules, relationships, data volumes, and integrations
5. Questions should be conversational and easy to understand — not technical
6. Avoid asking questions already answered in the conversation
7. Assess your current understanding confidence (0-100%)

CRITICAL: Questions must be SPECIFIC to what the user has described. Do NOT ask generic questions like "what is the purpose?" — you already have the initial description.
Instead, drill down into specifics: Who uses what? What happens when X occurs? How does Y relate to Z?

Round-specific guidance:
- Round 1: Ask about core entities, users, and primary workflows
- Round 2: Ask about relationships, edge cases, and business rules  
- Round 3: Ask about scale, integrations, notifications, and compliance
- Round 4+: Ask about very specific operational scenarios and exceptions

Return ONLY valid JSON with this exact structure:
{
  "understood_so_far": "2-3 sentence summary of what you understand about this project so far",
  "confidence": 65,
  "key_gaps": ["gap 1", "gap 2"],
  "questions": [
    {
      "id": 1,
      "question": "The actual question to ask",
      "why_important": "Brief reason this matters for the schema"
    }
  ],
  "ready_for_blueprint": false
}

Set ready_for_blueprint to true ONLY if confidence >= 85 AND all major aspects are covered."""

    user_prompt = f"""Domain detected: {domain}
Clarification round: {round_number}

CONVERSATION SO FAR:
{conversation_transcript}

QUESTIONS ALREADY ASKED (do NOT repeat these):
{asked_questions}

Based on the above, generate the next best set of clarifying questions to fill the gaps in your understanding.
Focus on what you DON'T yet know that would significantly impact the database design."""

    try:
        response = generate_schema(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        content = response["content"].strip()

        # Strip markdown fences
        if "```" in content:
            parts = content.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    content = part
                    break

        data = json.loads(content)
        logger.info(
            f"🧠 Dynamic questions generated — confidence: {data.get('confidence', 0)}% "
            f"| ready: {data.get('ready_for_blueprint', False)}"
        )
        return data

    except Exception as e:
        logger.error(f"Dynamic question generation failed: {e}")
        # Fallback to basic questions
        return {
            "understood_so_far": "I understand you want to build a database system.",
            "confidence": 40,
            "key_gaps": ["scale", "users", "core entities"],
            "questions": [
                {
                    "id": 1,
                    "question": "Who are the main types of users of this system, and what are their primary roles?",
                    "why_important": "Determines the user management and permission structure"
                },
                {
                    "id": 2,
                    "question": "What is the single most important workflow this system needs to support?",
                    "why_important": "Identifies the core transaction tables needed"
                },
                {
                    "id": 3,
                    "question": "Roughly how many records do you expect per day — hundreds, thousands, or millions?",
                    "why_important": "Determines indexing strategy and table partitioning"
                }
            ],
            "ready_for_blueprint": False
        }


def assess_understanding(state: ConversationState, domain: str) -> dict:
    """
    After each user answer, ask the AI to assess how well it now understands the project
    and summarise the accumulated knowledge.
    """
    from app.services.ai_service import generate_schema

    conversation_transcript = state.get_conversation_so_far()

    system_prompt = """You are a database architect assessing your understanding of a client's project.
Review the conversation and determine:
1. What you now understand clearly
2. What is still ambiguous or missing
3. Your overall confidence level (0-100%)

Return ONLY valid JSON:
{
  "understood": {
    "project_type": "...",
    "core_entities": ["entity1", "entity2"],
    "user_types": ["admin", "customer"],
    "key_workflows": ["workflow1"],
    "scale": "small|medium|large|unknown",
    "special_requirements": ["gst", "notifications", etc],
    "integrations": ["payment gateway", etc]
  },
  "still_unclear": ["aspect1", "aspect2"],
  "confidence": 72,
  "one_line_summary": "A 10-word summary of the project"
}"""

    user_prompt = f"""Conversation transcript:
{conversation_transcript}

Assess your understanding."""

    try:
        response = generate_schema(system_prompt=system_prompt, user_prompt=user_prompt)
        content = response["content"].strip()
        if "```" in content:
            parts = content.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    content = part
                    break
        return json.loads(content)
    except Exception as e:
        logger.error(f"Understanding assessment failed: {e}")
        return {"confidence": 50, "one_line_summary": "Project under analysis", "understood": {}, "still_unclear": []}