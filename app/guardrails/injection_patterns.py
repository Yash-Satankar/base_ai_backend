# app/guardrails/injection_patterns.py
"""
Prompt-injection / jailbreak pattern catalogue.

Pure data + a ``scan()`` function, no app imports, so it is trivially
unit-testable and reusable. Each entry is ``(category, severity, pattern)``
where severity is:
    "high"   — an unambiguous instruction-override or extraction attempt
    "medium" — suspicious framing that only counts when it stacks up

The input gate treats a single "high" hit, or two or more "medium" hits, as
an injection attempt.
"""

import re
from dataclasses import dataclass

_PATTERNS: list[tuple[str, str, str]] = [
    # ── instruction override ──────────────────────────────────────
    ("instruction_override", "high",
     r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|preceding|earlier|prior)\s+"
     r"(?:instructions?|prompts?|messages?|context|rules?|directions?)"),
    ("instruction_override", "high",
     r"disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous\s+|prior\s+|above\s+|system\s+)?"
     r"(?:instructions?|prompts?|rules?|guidelines?|context)"),
    ("instruction_override", "high",
     r"forget\s+(?:everything|all|your|the|any)\s+"
     r"(?:above|previous|prior|instructions?|rules?|guidelines?|training|context)"),
    ("instruction_override", "high",
     r"do\s+not\s+(?:follow|obey|adhere\s+to|comply\s+with)\s+(?:your|the|any|these)\s+"
     r"(?:instructions?|rules?|guidelines?|constraints?)"),
    ("instruction_override", "medium", r"\bnew\s+instructions?\s*[:\-]"),
    ("instruction_override", "medium", r"\boverride\s+(?:your|the|all|any)\s+(?:instructions?|settings?|rules?|behaviou?r)"),
    ("instruction_override", "medium", r"\bstop\s+being\s+(?:a|an)\b"),

    # ── system-prompt / rules extraction ─────────────────────────
    ("prompt_extraction", "high",
     r"(?:what|show|tell|give|print|repeat|reveal|display|output|reproduce|share)\s+"
     r"(?:me\s+)?(?:your|the)\s+(?:system\s+|initial\s+|original\s+)?"
     r"(?:prompt|instructions?|rules?|guidelines?|configuration|config|directives?)"),
    ("prompt_extraction", "high",
     r"repeat\s+(?:the\s+)?(?:words?|text|everything|content|message)\s+"
     r"(?:above|before|preceding|prior|that\s+came\s+before)"),
    ("prompt_extraction", "high",
     r"(?:print|output|show|give)\s+(?:me\s+)?(?:everything|the\s+text|all\s+text|the\s+content)\s+"
     r"(?:above|before\s+this|preceding)"),
    ("prompt_extraction", "medium",
     r"what\s+(?:are|is)\s+your\s+(?:rules?|guidelines?|constraints?|restrictions?|directives?|limitations?)"),
    ("prompt_extraction", "medium",
     r"how\s+(?:were|are)\s+you\s+(?:instructed|configured|programmed|trained|set\s+up|told\s+to)"),
    ("prompt_extraction", "high",
     r"\blist\s+(?:all\s+|out\s+)?(?:of\s+)?(?:your|the)\s+(?:rules?|guidelines?|instructions?|directives?)\b"),

    # ── role-play / persona hijack ──────────────────────────────
    ("roleplay_hijack", "medium", r"you\s+are\s+now\s+(?:a|an|the|going\s+to|going\s+to\s+be|in)\b"),
    ("roleplay_hijack", "medium", r"(?:act|behave|respond|talk)\s+(?:as|like)\s+(?:if\s+you\s+(?:are|were)\s+|a\s+|an\s+)"),
    ("roleplay_hijack", "medium", r"pretend\s+(?:you\s+are|you're|to\s+be|that\s+you)"),
    ("roleplay_hijack", "high",   r"\b(?:DAN|do\s+anything\s+now)\b"),
    ("roleplay_hijack", "high",   r"(?:developer|debug|god|admin|root|sudo|jailbreak|unrestricted)\s+mode\b"),
    ("roleplay_hijack", "medium", r"you\s+have\s+no\s+(?:restrictions?|rules?|limits?|filters?|guidelines?|guardrails?)"),
    ("roleplay_hijack", "medium", r"\byou\s+are\s+not\s+(?:an?\s+)?(?:ai|assistant|bound|restricted|limited)"),
    ("roleplay_hijack", "medium", r"from\s+now\s+on\b[,\s]+you\s+(?:will|must|are|should|can)"),
    ("roleplay_hijack", "medium", r"\bwithout\s+any\s+(?:restrictions?|filters?|limitations?|rules?)"),

    # ── fake system framing / control tokens ────────────────────
    ("fake_system", "high",   r"<\|?(?:im_start|im_end|im_sep|system|endoftext|assistant|user)\|?>"),
    ("fake_system", "high",   r"\[/?INST\]"),
    ("fake_system", "medium", r"^\s*(?:system|assistant)\s*[:\-]\s+\S"),
    ("fake_system", "medium", r"\[\s*(?:system|instructions?|prompt)\s*\]"),
    ("fake_system", "medium", r"#{2,}\s*(?:system|instruction|new\s+prompt|context)\b"),
    ("fake_system", "medium", r"\bsystem\s*prompt\s*[:=]"),
]

_FLAGS = re.IGNORECASE | re.MULTILINE
_COMPILED = [(c, s, re.compile(p, _FLAGS)) for (c, s, p) in _PATTERNS]


@dataclass(frozen=True)
class Match:
    category: str
    severity: str          # "high" | "medium"
    snippet: str


def scan(text: str) -> list[Match]:
    """Return every distinct pattern that fires against ``text``."""
    if not text:
        return []
    out: list[Match] = []
    for cat, sev, rx in _COMPILED:
        m = rx.search(text)
        if m:
            out.append(Match(cat, sev, m.group(0)[:80]))
    return out


def worst_severity(matches: list[Match]) -> str | None:
    if any(m.severity == "high" for m in matches):
        return "high"
    if matches:
        return "medium"
    return None
