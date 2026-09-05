# app/engine/decomposition_signals.py
"""
Detects an EXPLICIT organizational signal in a user's requirement text that
schema decomposition (splitting the project into multiple bounded-context
schemas — see docs/enterprise_standards_spec.md §2.2) might be worth asking
about.

Deliberately conservative and deterministic: no source in the enterprise
standards research supports inferring a schema split from table count, or
from an LLM's own unprompted read of ambiguous domain language. This only
fires on phrases that name a real organizational/product boundary — the
kind of thing DDD's bounded-context literature (Fowler, Evans) and Conway's
Law actually point to as the real driver, not a vague "this seems complex"
heuristic. A hit here is a reason to ASK the user, never a reason to decide
for them — see the SCHEMA_DECOMPOSITION_ENABLED gate in
app/conversation/turn_loop.py's clarifying flow.
"""

import re

_SIGNAL_PATTERNS = [
    r"\bseparate\s+(schemas?|databases?)\b",
    r"\bown\s+(schema|database)\b",
    r"\bindependent(ly)?\s+(teams?|deployed|deployable|owned|operated)\b",
    r"\b(different|multiple|separate)\s+teams?\b",
    r"\bdifferent\s+departments?\b",
    r"\bsplit\s+(?:\w+\s+){0,2}(into|across)\s+(services?|schemas?|databases?)\b",
    r"\bmulti[- ]?service\b",
    r"\bmicroservices?\b",
    r"\bmultiple\s+products?\b",
    r"\beach\s+team\s+(owns|manages|runs)\b",
    r"\boperate\s+independently\b",
]
_SIGNAL_RE = re.compile("|".join(_SIGNAL_PATTERNS), re.IGNORECASE)


def detect_decomposition_signal(text: str) -> bool:
    """True when `text` names a real organizational/product boundary that
    plausibly maps to separate bounded-context schemas. False (including for
    empty/None input) otherwise — err toward not asking rather than asking
    on thin evidence."""
    if not text:
        return False
    return bool(_SIGNAL_RE.search(text))
