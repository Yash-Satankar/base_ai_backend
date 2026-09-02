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
    COMPILING       = "compiling"         # L1-L8 blueprint compile running as an async job
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

    # Phase 2 — working memory / lean loop
    rolling_summary: str = ""                                   # compacted older turns
    key_decisions: list = field(default_factory=list)           # extracted commitments the user made
    rejected_options: list = field(default_factory=list)        # things the user explicitly turned down
    facts: dict = field(default_factory=dict)                   # per-turn caches + flags (domain, language, degrade)

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


# ── Clarification: one structured call per turn ─────────────────
#
# Phase 2 merges what used to be two LLM round-trips per clarifying turn
# (assess_understanding + generate_dynamic_clarifying_questions) into a
# single structured call. One call = lower cost and no chance of the model
# contradicting itself between the two.

def _strip_json(content: str) -> dict:
    content = (content or "").strip()
    if "```" in content:
        for part in content.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                content = part
                break
    return json.loads(content)


_CLARIFY_SYSTEM = """You are a database design partner in a discovery conversation with a client.
In ONE pass you must both (a) assess how well you now understand the project and
(b) ask the next most valuable clarifying questions.

Rules for the questions:
- 3-4 by default; SPECIFIC to what the user has actually described, never generic
- drill into edge cases, business rules, relationships, data volumes, integrations
- conversational language, not technical; never repeat a question already asked

Round guidance: R1 core entities/users/workflows · R2 relationships/edge cases/rules ·
R3 scale/integrations/compliance · R4+ operational exceptions.

Return ONLY valid JSON, exactly this shape:
{
  "understood_so_far": "2-3 sentence summary of the project as you understand it now",
  "one_line_summary": "a ~10-word summary",
  "confidence": 65,
  "understood": {
    "project_type": "...", "core_entities": [], "user_types": [],
    "key_workflows": [], "scale": "small|medium|large|unknown",
    "special_requirements": [], "integrations": []
  },
  "key_gaps": ["gap 1", "gap 2"],
  "questions": [
    {"id": 1, "question": "the question", "why_important": "why it matters for the schema"}
  ],
  "ready_for_blueprint": false
}
Set ready_for_blueprint true ONLY when confidence >= 85 and every major aspect is covered."""


def run_clarify_turn(
    state: "ConversationState",
    domain: str,
    transcript: str,
    *,
    round_number: int,
    degrade: bool = False,
    language_ack: Optional[str] = None,
) -> dict:
    """
    One structured LLM call that both assesses understanding and produces the
    next clarifying questions. Returns the merged dict (see _CLARIFY_SYSTEM).
    Falls back to a safe generic set on any failure.
    """
    from app.conversation.llm_client import call_llm

    asked = "\n".join(f"- {q}" for q in state.questions_asked) if state.questions_asked else "None yet"

    system_prompt = _CLARIFY_SYSTEM
    if degrade:
        system_prompt += (
            "\n\nBe efficient: ask at most 2 questions, and if you already have a "
            "workable picture set ready_for_blueprint to true."
        )
    if language_ack:
        system_prompt += "\n\n" + language_ack

    user_prompt = f"""Domain: {domain}
Clarification round: {round_number}

CONVERSATION SO FAR:
{transcript}

QUESTIONS ALREADY ASKED (do NOT repeat):
{asked}

Assess your understanding and ask the next best questions."""

    try:
        response = call_llm(
            operation="clarify_turn",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            session_id=state.session_id,
            project_id=state.project_id,
            degrade=degrade,
        )
        data = _strip_json(response["content"])
        logger.info(
            f"🧠 Clarify turn — confidence: {data.get('confidence', 0)}% "
            f"| ready: {data.get('ready_for_blueprint', False)} | degrade: {degrade}"
        )
        return data
    except Exception as e:
        logger.error(f"Clarify turn failed: {e}")
        return {
            "understood_so_far": "I understand you want to build a database system.",
            "one_line_summary": "Project under analysis",
            "confidence": 40,
            "understood": {},
            "key_gaps": ["scale", "users", "core entities"],
            "questions": [
                {"id": 1, "question": "Who are the main types of users, and what do they each do?",
                 "why_important": "Determines the user and permission structure"},
                {"id": 2, "question": "What is the single most important workflow this system must support?",
                 "why_important": "Identifies the core transaction tables"},
                {"id": 3, "question": "Roughly how many records per day — hundreds, thousands, or millions?",
                 "why_important": "Determines indexing and partitioning strategy"},
            ],
            "ready_for_blueprint": False,
        }