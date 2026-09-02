# app/guardrails/output_gate.py
"""
Final coherence & safety pass on any assistant message before it is sent.

Phase 1 — deterministic only:
  - redact leaked internal identifiers (leakage.redact)
  - structural checks: empty / stub text, unbalanced markdown code fence,
    obviously broken embedded JSON
  - on an unrecoverable structural problem, swap in a canned in-persona line

No contradiction detection and no LLM repair retry — those are later phases.
Every catch is logged so failure patterns stay visible.
"""

import re
import logging
from dataclasses import dataclass, field

from app.guardrails import leakage
from app.prompts import persona

logger = logging.getLogger(__name__)

_STUBS = {"", ".", "...", "…", "n/a", "na", "todo", "tbd", "[response]", "response",
          "null", "none", "undefined", "message"}


@dataclass
class GuardResult:
    message: str
    action: str = "pass"                 # "pass" | "redacted" | "replaced"
    reasons: list = field(default_factory=list)


def check_and_repair(message: str, state=None, *, fallback_key: str = "turn_error") -> GuardResult:
    reasons: list[str] = []
    text = message or ""

    # ── empty / template stub ──────────────────────────────────
    if text.strip().lower() in _STUBS:
        logger.warning("output_gate: replaced empty/stub response")
        return GuardResult(persona.fallback(fallback_key), "replaced", ["empty_or_stub"])

    # ── leaked internal identifiers → deterministic redaction ──
    leaks = leakage.scan(text)
    if leaks:
        text = leakage.redact(text)
        reasons.append("leaked_identifier:" + ",".join(sorted({l.category for l in leaks})))

    # ── unbalanced markdown code fence → close it ──────────────
    if text.count("```") % 2 == 1:
        text = text.rstrip() + "\n```"
        reasons.append("unbalanced_code_fence")

    # ── obviously broken embedded JSON object ─────────────────
    if "{" in text and re.search(r'"\s*:\s*', text) and abs(text.count("{") - text.count("}")) >= 1:
        logger.warning("output_gate: replaced response with broken embedded JSON")
        return GuardResult(persona.fallback(fallback_key), "replaced", reasons + ["broken_json"])

    # ── nothing meaningful left after redaction ───────────────
    if text.strip().lower() in _STUBS:
        return GuardResult(persona.fallback(fallback_key), "replaced", reasons + ["empty_after_redaction"])

    if reasons:
        logger.warning(f"output_gate: redacted response ({reasons})")
        return GuardResult(text, "redacted", reasons)

    return GuardResult(text, "pass", [])


def guard_text(message: str, state=None, *, fallback_key: str = "turn_error") -> str:
    """Convenience wrapper — returns the safe message string."""
    return check_and_repair(message, state, fallback_key=fallback_key).message
