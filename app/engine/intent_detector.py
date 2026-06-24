# app/engine/intent_detector.py

import re
from dataclasses import dataclass
from typing import Optional
from app.engine.conversation_engine import ConversationState, ConversationStage


@dataclass
class Intent:
    type: str
    confidence: float           # 0.0 - 1.0
    sub_action: Optional[str] = None   # "add" | "remove" | "rename" | "replace"
    extracted_content: Optional[str] = None  # what they want to add/change
    is_question: bool = False


# ── Intent type constants ────────────────────────────────────────
class IntentType:
    CONFIRM             = "confirm"
    CONFIRM_WITH_CHANGE = "confirm_with_change"
    EDIT                = "edit"
    ADD                 = "add"
    REMOVE              = "remove"
    START_OVER          = "start_over"
    REGENERATE          = "regenerate"
    QUESTION            = "question"
    PASTE_SQL           = "paste_sql"
    CONTEXT_SWITCH      = "context_switch"
    AMBIGUOUS           = "ambiguous"
    NORMAL              = "normal"
    DOWNLOAD            = "download"
    EXPLAIN             = "explain"


# ── Keyword maps ─────────────────────────────────────────────────

START_OVER_SIGNALS = [
    "start over", "start fresh", "start again", "reset",
    "new project", "forget it", "forget everything",
    "scratch that", "never mind", "scrap this",
    "begin again", "different project",
]

CONFIRM_SIGNALS = [
    "yes", "yep", "yeah", "yup", "correct", "confirmed",
    "confirm", "ok", "okay", "looks good", "looks correct",
    "that's right", "that's correct", "proceed", "go ahead",
    "approve", "approved", "perfect", "great", "sounds good",
    "exactly", "right", "fine", "done", "agreed",
]

EDIT_SIGNALS = [
    "edit", "change", "modify", "update", "fix",
    "rename", "replace", "swap", "switch",
    "instead", "rather", "actually",
]

ADD_SIGNALS = [
    "add", "include", "also", "additionally",
    "dont forget", "don't forget", "missing",
    "need also", "also need", "we need",
    "plus", "as well", "too",
]

REMOVE_SIGNALS = [
    "remove", "delete", "drop", "exclude",
    "don't need", "dont need", "not needed",
    "skip", "ignore", "without",
]

REGENERATE_SIGNALS = [
    "regenerate", "redo", "try again", "generate again",
    "do it again", "another attempt", "retry",
    "not happy", "don't like", "dont like",
    "different schema", "try differently",
]

DOWNLOAD_SIGNALS = [
    "download", "get file", "give me the file",
    "send file", "export", "get sql", "get pdf",
]

EXPLAIN_SIGNALS = [
    "explain", "what is", "tell me about",
    "what does", "why is", "how does",
    "describe", "clarify",
]

QUESTION_STARTERS = [
    "what", "how", "why", "when", "where", "who",
    "which", "is it", "can i", "should i", "do i",
    "will it", "does it", "can you explain",
]


def detect_intent(
    message: str,
    state: ConversationState,
) -> Intent:
    """
    Detect the user's intent from their message.
    Takes current state into account for context.
    Returns an Intent object.
    """
    msg_lower = message.lower().strip()
    msg_clean = re.sub(r'[^\w\s]', ' ', msg_lower)
    words = msg_clean.split()
    words_count = len(words)

    # ── Check for SQL paste ──────────────────────────────────────
    if re.search(r'create\s+table', msg_lower, re.IGNORECASE):
        return Intent(
            type=IntentType.PASTE_SQL,
            confidence=0.95,
            extracted_content=message,
        )

    # ── Check for start over ─────────────────────────────────────
    if _contains_phrase(msg_lower, START_OVER_SIGNALS) and words_count <= 5 and state.stage != ConversationStage.INITIAL:
        return Intent(
            type=IntentType.START_OVER,
            confidence=0.9,
        )

    # ── Check for download ───────────────────────────────────────
    if _contains_phrase(msg_lower, DOWNLOAD_SIGNALS):
        # Only treat as download if we are in the complete stage or it is a very short direct command
        if state.stage == ConversationStage.COMPLETE or words_count <= 4:
            file_type = "both"
            if "sql" in msg_lower and "pdf" not in msg_lower:
                file_type = "sql"
            elif "pdf" in msg_lower and "sql" not in msg_lower:
                file_type = "pdf"
            return Intent(
                type=IntentType.DOWNLOAD,
                confidence=0.9,
                sub_action=file_type,
            )

    # ── Check for explain request ────────────────────────────────
    if _contains_phrase(msg_lower, EXPLAIN_SIGNALS) and words_count <= 10:
        # Extract what they want explained
        table_match = re.search(r'(\w+_(?:header|transaction|archive|life_cycle|configuration)_all)', message)
        content = table_match.group(1) if table_match else message
        return Intent(
            type=IntentType.EXPLAIN,
            confidence=0.85,
            extracted_content=content,
            is_question=True,
        )

    # ── Check for question ───────────────────────────────────────
    is_question = (
        message.strip().endswith("?") or
        _starts_with_any(msg_clean, QUESTION_STARTERS)
    )

    # ── Check for regenerate ─────────────────────────────────────
    if _contains_phrase(msg_lower, REGENERATE_SIGNALS) and words_count <= 5 and state.stage != ConversationStage.INITIAL:
        return Intent(
            type=IntentType.REGENERATE,
            confidence=0.85,
        )

    # ── Pure confirmation ────────────────────────────────────────
    if words_count <= 3 and _contains_phrase(msg_lower, CONFIRM_SIGNALS):
        return Intent(
            type=IntentType.CONFIRM,
            confidence=0.95,
        )

    # ── Confirmation WITH changes ────────────────────────────────
    has_confirm = _contains_phrase(msg_lower, CONFIRM_SIGNALS)
    has_add     = _contains_phrase(msg_lower, ADD_SIGNALS)
    has_edit    = _contains_phrase(msg_lower, EDIT_SIGNALS)
    has_remove  = _contains_phrase(msg_lower, REMOVE_SIGNALS)

    if has_confirm and (has_add or has_edit or has_remove):
        sub = "add" if has_add else "edit" if has_edit else "remove"
        return Intent(
            type=IntentType.CONFIRM_WITH_CHANGE,
            confidence=0.88,
            sub_action=sub,
            extracted_content=_extract_change_content(message),
        )

    # ── Edit only ────────────────────────────────────────────────
    if has_edit and not has_confirm:
        return Intent(
            type=IntentType.EDIT,
            confidence=0.85,
            sub_action="edit",
            extracted_content=_extract_change_content(message),
        )

    # ── Add only ─────────────────────────────────────────────────
    if has_add and not has_confirm:
        return Intent(
            type=IntentType.ADD,
            confidence=0.85,
            sub_action="add",
            extracted_content=_extract_change_content(message),
        )

    # ── Remove only ──────────────────────────────────────────────
    if has_remove and not has_confirm:
        return Intent(
            type=IntentType.REMOVE,
            confidence=0.85,
            sub_action="remove",
            extracted_content=_extract_change_content(message),
        )

    # ── Question ─────────────────────────────────────────────────
    if is_question:
        return Intent(
            type=IntentType.QUESTION,
            confidence=0.8,
            is_question=True,
            extracted_content=message,
        )

    # ── Check for context switch ─────────────────────────────────
    if state.blueprint and state.stage not in [
        ConversationStage.INITIAL, ConversationStage.COMPLETE
    ]:
        if _is_context_switch(message, state):
            return Intent(
                type=IntentType.CONTEXT_SWITCH,
                confidence=0.75,
                extracted_content=message,
            )

    # ── Ambiguous short message ──────────────────────────────────
    if len(words) <= 2 and not has_confirm:
        return Intent(
            type=IntentType.AMBIGUOUS,
            confidence=0.6,
            extracted_content=message,
        )

    # ── Default: normal response ─────────────────────────────────
    return Intent(
        type=IntentType.NORMAL,
        confidence=0.7,
        extracted_content=message,
    )


# ── Helpers ──────────────────────────────────────────────────────

def _contains_phrase(text: str, phrases: list[str]) -> bool:
    for phrase in phrases:
        if phrase in text:
            return True
    return False


def _starts_with_any(text: str, starters: list[str]) -> bool:
    for starter in starters:
        if text.startswith(starter):
            return True
    return False


def _extract_change_content(message: str) -> str:
    """
    Extract the actual change content from a message.
    Example: "YES but also add an exam module" → "add an exam module"
    """
    # Remove confirmation words from start
    cleaned = message.strip()
    for confirm in ["yes,", "yes ", "okay,", "okay ", "ok,", "ok "]:
        if cleaned.lower().startswith(confirm):
            cleaned = cleaned[len(confirm):].strip()
            break

    # Remove filler words
    fillers = ["but ", "however ", "also ", "please ", "could you ", "can you "]
    for filler in fillers:
        if cleaned.lower().startswith(filler):
            cleaned = cleaned[len(filler):].strip()

    return cleaned


def _is_context_switch(message: str, state: ConversationState) -> bool:
    """
    Detect if user is switching to a completely different project domain.
    """
    if not state.blueprint:
        return False

    from app.engine.rule_matcher import DOMAIN_KEYWORDS

    current_domain = state.blueprint.domain
    current_keywords = set(DOMAIN_KEYWORDS.get(current_domain, []))

    # Check how many keywords from OTHER domains appear
    msg_lower = message.lower()
    other_domain_hits = 0
    current_domain_hits = sum(1 for kw in current_keywords if kw in msg_lower)

    for domain, keywords in DOMAIN_KEYWORDS.items():
        if domain == current_domain:
            continue
        hits = sum(1 for kw in keywords if kw in msg_lower)
        other_domain_hits += hits

    # Context switch if other domains have significantly more hits
    return other_domain_hits > current_domain_hits + 3