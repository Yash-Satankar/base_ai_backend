# app/engine/conversation_engine.py

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional
import json
import logging

logger = logging.getLogger(__name__)


class ConversationStage(str, Enum):
    INITIAL         = "initial"           # User just described project
    CLARIFYING      = "clarifying"        # AI asking questions
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
    schema: Optional[str] = None
    validation_score: Optional[int] = None
    fix_attempts: int = 0
    sql_file_path: Optional[str] = None
    pdf_file_path: Optional[str] = None

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
# ── Clarifying questions per domain ─────────────────────────────

CLARIFYING_QUESTIONS = {
    "financial": [
        "Will this system handle Indian GST invoicing?",
        "Do you need a wallet or running balance feature?",
        "Will there be multiple payment modes (cash, UPI, card)?",
    ],
    "hr": [
        "Do you need attendance tracking and leave management?",
        "Will this handle salary/payroll calculations?",
        "Do you need multi-level approval workflows?",
    ],
    "e_learning": [
        "Do you need student fee collection and receipt generation?",
        "Will there be exam/quiz modules with scoring?",
        "Do you need batch or class management?",
    ],
    "security_agency": [
        "Do you need guard duty scheduling (monthly roster)?",
        "Will clients (campuses) be managed separately from the agency?",
        "Do you need salary calculation for guards?",
    ],
    "real_estate": [
        "Do you need broker-to-broker property sharing?",
        "Will you track both sale and rental listings?",
        "Do you need client requirement matching?",
    ],
    "e_commerce": [
        "Do you need inventory/stock management?",
        "Will there be returns and refund workflows?",
        "Do you need GST invoicing on orders?",
    ],
    "multi_tenant_saas": [
        "How many tenants do you expect? (10s, 100s, 1000s)",
        "Do tenants need isolated data or shared tables?",
        "Will you have subscription plans and billing?",
    ],
    "general": [
        "What is the primary purpose of this database?",
        "Roughly how many users will this system handle?",
        "Do you need any Indian compliance (GST, etc.)?",
    ],
}

SCALE_QUESTIONS = [
    "What scale are you expecting?",
    "  A) Small — up to 1,000 records per table",
    "  B) Medium — up to 100,000 records per table",
    "  C) Large — millions of records, high concurrency",
]


def get_clarifying_questions(domain: str, existing_answers: str) -> list[str]:
    """Get relevant questions for the detected domain."""
    domain_questions = CLARIFYING_QUESTIONS.get(domain, CLARIFYING_QUESTIONS["general"])
    
    # Always include scale if not mentioned
    all_questions = domain_questions.copy()
    scale_keywords = ["small", "medium", "large", "users", "records", "scale"]
    if not any(kw in existing_answers.lower() for kw in scale_keywords):
        all_questions.append("\n".join(SCALE_QUESTIONS))

    return all_questions[:4]