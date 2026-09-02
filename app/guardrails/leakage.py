# app/guardrails/leakage.py
"""
Detect and redact internal identifiers that must never reach a user:
rule IDs, pipeline-level field names, provider/model names, internal symbol
names, tracebacks, and raw HTTP error phrasing.

The list is deliberately tight and high-confidence. Bare English words like
"blueprint", "complete", or "clarifying" are NOT redacted — only their
internal / machine forms (``l1_understanding``, ``ConversationStage.CLARIFYING``,
``"stage": "clarifying"``) are.
"""

import re
from dataclasses import dataclass

# (category, pattern, replacement)
_RULES: list[tuple[str, str, str]] = [
    # ── proprietary rule references ──────────────────────────────
    ("rule_id",  r"\brule\s*#?\s*\d{1,3}\b",              "our design guidelines"),
    ("rule_id",  r"\brule[_\s]?id[s]?\b",                 "guideline references"),
    ("rule_id",  r"\b\d{1,3}\s+(?:critical|high|medium|low)\s+priority\s+rules?\b", "several design guidelines"),

    # ── abstraction pipeline internals ──────────────────────────
    ("pipeline", r"\bL[1-8][_\s]?(?:understanding|capabilit(?:y|ies)|workflows?|entit(?:y|ies)|"
                 r"relationships?|life[_\s]?cycles?|modules?)\b",                 "the design analysis"),
    ("pipeline", r"\bl[1-8]_(?:understanding|capabilities|workflows|entities|relationships|lifecycles|modules)\b",
                 "design analysis"),
    ("pipeline", r"\babstraction\s+(?:pipeline|engine|levels?|layers?)\b",       "the design process"),
    ("pipeline", r"\b(?:L1|L2|L3|L4|L5|L6|L7|L8)\s*(?:->|→|to)\s*(?:L1|L2|L3|L4|L5|L6|L7|L8)\b",
                 "the design process"),

    # ── provider / model / infra names ─────────────────────────
    ("provider", r"\bgroq\b",                             "the model"),
    ("provider", r"\bllama[-\s]?[\d.]+[-\w]*\b",          "the model"),
    ("provider", r"\banthropic\b",                        "the model"),
    ("provider", r"\bclaude[-\s][\w.\-]+\b",              "the model"),
    ("provider", r"\bqwen[\w./\-]*\b",                    "the model"),
    ("provider", r"\bgpt-oss[\w\-]*\b",                   "the model"),
    ("provider", r"\bqdrant\b",                           "the search index"),
    ("provider", r"\ball-minilm[\w\-]*\b",                "the embedding model"),

    # ── internal symbol / module names ─────────────────────────
    ("internal", r"\b_handle_[a-z_]+\b",                  "this step"),
    ("internal", r"\b(?:process_message|run_fix_pass|detect_intent|generate_schema|assess_input)\b", "this step"),
    ("internal", r"\b(?:TelemetryManager|SchemaValidator|ConversationEngine|ConversationState|"
                 r"ConversationStage|IntentType|ProjectBlueprint)\b",            "the system"),
    ("internal", r"\b(?:conversation_engine|intent_detector|intent_handlers|abstraction_pipeline|"
                 r"architecture_planner|planner_service|rule_matcher|schema_validator|rule_service)\b", "the system"),
    ("internal", r"\bsystem[_\s]prompt\b",                "my instructions"),

    # ── raw errors / tracebacks / HTTP phrasing ────────────────
    ("error",    r"traceback \(most recent call last\):[\s\S]*",  ""),
    ("error",    r'\bfile "[^"]+", line \d+[^\n]*',               ""),
    ("error",    r"\bhttpexception\b",                            "a problem"),
    ("error",    r"\bstatus[_\s]?code[=:\s]+\d{3}\b",             "an error"),
    ("error",    r"\b[45]\d\d\s+(?:internal\s+server\s+error|bad\s+gateway|service\s+unavailable|gateway\s+timeout)\b",
                 "a temporary problem"),

    # ── conversation-stage machine leaks ───────────────────────
    ("stage",    r"\bconversationstage\.\w+\b",                   "this step"),
    ("stage",    r'"stage"\s*:\s*"(?:initial|clarifying|blueprint|confirmed|generating|fixing|complete)"', ""),
]

_COMPILED = [(c, re.compile(p, re.IGNORECASE), r) for (c, p, r) in _RULES]


@dataclass(frozen=True)
class Leak:
    category: str
    snippet: str


def scan(text: str) -> list[Leak]:
    """Return every internal-identifier category that appears in ``text``."""
    if not text:
        return []
    out: list[Leak] = []
    for cat, rx, _repl in _COMPILED:
        m = rx.search(text)
        if m:
            out.append(Leak(cat, m.group(0)[:80]))
    return out


def redact(text: str) -> str:
    """Replace every internal identifier with a neutral phrase."""
    if not text:
        return text
    for _cat, rx, repl in _COMPILED:
        text = rx.sub(repl, text)
    # tidy whitespace / punctuation left behind by deletions
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
