# app/guardrails/output_gate.py
"""
Final coherence & safety pass on any assistant message before it is sent.

Deterministic only:
  - redact leaked internal identifiers (leakage.redact)
  - structural checks: empty / stub text, unbalanced markdown code fence,
    obviously broken embedded JSON
  - contradiction checks against the session facts ledger (Phase 3):
    domain flip, table-count flip, compliance-flag flip
  - on an unrecoverable problem, swap in a canned in-persona line

No LLM repair retry — that is Phase 4. Every catch is logged.
"""

import re
import logging
from dataclasses import dataclass, field

from app.guardrails import leakage
from app.prompts import persona

logger = logging.getLogger(__name__)

_STUBS = {"", ".", "...", "…", "n/a", "na", "todo", "tbd", "[response]", "response",
          "null", "none", "undefined", "message"}

# label -> domain key, for the domain-flip check
_DOMAIN_LABELS = {
    "ecommerce": "e_commerce", "e commerce": "e_commerce", "e-commerce": "e_commerce",
    "healthcare": "healthcare", "health care": "healthcare", "medical": "healthcare",
    "hospital": "healthcare",
    "logistics": "logistics", "shipping": "logistics", "fleet": "logistics",
    "real estate": "real_estate", "realty": "real_estate", "property": "real_estate",
    "hr": "hr", "human resources": "hr", "payroll": "hr",
    "financial": "financial", "fintech": "financial", "banking": "financial",
    "lending": "financial",
    "elearning": "e_learning", "e learning": "e_learning", "e-learning": "e_learning",
    "education": "e_learning", "lms": "e_learning",
    "saas": "multi_tenant_saas", "multi tenant": "multi_tenant_saas",
    "multi-tenant": "multi_tenant_saas",
    "security agency": "security_agency",
    "corporate": "corporate_enterprise", "enterprise": "corporate_enterprise",
}

_DOMAIN_CLAIM_RE = re.compile(
    r"\b(?:this is|it'?s|that'?s|you(?:'?re| are) building|building|designing)\s+"
    r"(?:a|an)\s+([a-z][\w\- ]{1,28}?)\s+"
    r"(?:project|system|schema|database|app|platform|product)\b",
    re.IGNORECASE,
)
_TABLE_COUNT_RE = re.compile(
    r"(?:~|≈|approximately|about|around|roughly)?\s*(\d{1,3})\s+tables?\b",
    re.IGNORECASE,
)
_GST_DROP_RE = re.compile(
    r"\b(?:no|not|without|skip(?:ping)?|drop(?:ping)?)\s+gst\b"
    r"|\bgst\s+(?:is\s+)?(?:not\s+(?:required|needed|applicable)|off|excluded)\b"
    r"|\bnon-gst\b",
    re.IGNORECASE,
)
_GST_ADD_RE = re.compile(
    r"\b(?:with\s+)?(?:full\s+)?gst\s+(?:compliance|invoicing|support|enabled|required)\b"
    r"|\bgst\s+(?:is\s+)?required\b",
    re.IGNORECASE,
)


def _blueprint_table_count(bp) -> int | None:
    try:
        return sum(len(m.get("tables", [])) for m in bp.modules)
    except Exception:
        return None


def _contradiction_reasons(text: str, state) -> list[str]:
    """Deterministic contradictions with what the session already established."""
    if state is None or not text:
        return []
    reasons: list[str] = []
    low = text.lower()
    facts = getattr(state, "facts", {}) or {}
    bp = getattr(state, "blueprint", None)

    # ── domain flip ────────────────────────────────────────────
    known_domain = facts.get("_domain")
    if known_domain and known_domain != "general":
        m = _DOMAIN_CLAIM_RE.search(text)
        if m:
            phrase = re.sub(r"\s+", " ", m.group(1).strip().lower())
            claimed = _DOMAIN_LABELS.get(phrase) or (phrase.replace(" ", "_").replace("-", "_"))
            try:
                from app.engine.rule_matcher import DOMAIN_KEYWORDS
                is_known = claimed in DOMAIN_KEYWORDS
            except Exception:
                is_known = claimed in set(_DOMAIN_LABELS.values())
            if is_known and claimed != known_domain:
                reasons.append(f"contradiction:domain({known_domain}->{claimed})")

    # ── table-count flip (only once a blueprint exists) ────────
    ref = _blueprint_table_count(bp) if bp is not None else facts.get("_table_count")
    if ref and ref > 0:
        for n_str in _TABLE_COUNT_RE.findall(text):
            n = int(n_str)
            if abs(n - ref) > max(5, ref * 0.25):
                reasons.append(f"contradiction:table_count(ref={ref},said={n})")
                break

    # ── compliance-flag (GST) flip ────────────────────────────
    gst_ref = getattr(bp, "gst_required", None) if bp is not None else facts.get("_gst_required")
    if gst_ref is True and _GST_DROP_RE.search(low):
        reasons.append("contradiction:gst(required->dropped)")
    elif gst_ref is False and _GST_ADD_RE.search(low):
        reasons.append("contradiction:gst(not_required->added)")

    return reasons


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

    # ── contradiction with the session facts ledger (Phase 3) ──
    contradictions = _contradiction_reasons(text, state)
    if contradictions:
        logger.warning(f"output_gate: replaced contradictory response ({contradictions})")
        return GuardResult(persona.fallback("recheck"), "replaced", contradictions)

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
