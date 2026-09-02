# app/guardrails/input_gate.py
"""
Input gate: classify every incoming user message before it reaches the
conversation engine, and provide a safe, in-persona way to answer when the
message isn't a normal on-topic turn.

Phase 1 is rule-based only (no LLM tie-break). Non-topical categories are
answered with a canned in-persona redirect and never touch the engine or an
LLM. On-topic, ambiguous, and non-English input pass through unchanged
(non-English is best-effort; the in-persona language acknowledgement lands
with the lean turn loop in a later phase).

A per-session "quarantine" counter is kept in Redis. Past a threshold the
session is flagged for review (logged) — response quality is NOT degraded.
"""

import re
import logging
from dataclasses import dataclass

from app.guardrails import injection_patterns
from app.engine.rule_matcher import DOMAIN_KEYWORDS
from app.db.session_store import get_redis_client, mark_redis_down

logger = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────
CONVERSATION_MAX = 4000            # soft cap for a conversational turn
MIN_MEANINGFUL = 2                 # shorter than this carries no intent
QUARANTINE_FLAG_THRESHOLD = 5      # quarantined inputs before a session is flagged
_ABUSE_TTL = 86400                 # counter lifetime (seconds)


class Category:
    OK          = "ok"                  # normal on-topic turn
    AMBIGUOUS   = "ambiguous_on_topic"  # on-topic but thin — let the clarifier ask
    NON_ENGLISH = "non_english"         # pass through, best-effort
    EMPTY       = "empty"
    TOO_SHORT   = "too_short"
    TOO_LONG    = "too_long"
    MALFORMED   = "malformed"
    NONSENSE    = "nonsense"
    OFF_TOPIC   = "off_topic"
    INJECTION   = "prompt_injection"
    ABUSIVE     = "abusive"


# Categories that must NOT reach the conversation engine.
NON_TOPICAL = {
    Category.EMPTY, Category.TOO_SHORT, Category.TOO_LONG, Category.MALFORMED,
    Category.NONSENSE, Category.OFF_TOPIC, Category.INJECTION, Category.ABUSIVE,
}

# Category -> persona.FALLBACKS key
_REPLY_KEY = {
    Category.EMPTY:      "unclear",
    Category.TOO_SHORT:  "unclear",
    Category.TOO_LONG:   "too_long",
    Category.MALFORMED:  "unclear",
    Category.NONSENSE:   "off_topic",
    Category.OFF_TOPIC:  "off_topic",
    Category.INJECTION:  "deflect",
    Category.ABUSIVE:    "hostile",
}


@dataclass
class InputAssessment:
    category: str
    confidence: float
    sanitized_for_llm: str      # safe to place in a prompt
    sanitized_for_memory: str   # safe to persist to history
    quarantine: bool            # counts toward the session-abuse counter
    reply_key: str              # persona.FALLBACKS key for the redirect
    detail: str = ""

    @property
    def is_topical(self) -> bool:
        return self.category not in NON_TOPICAL


# ── Detectors ────────────────────────────────────────────────────

_SQL_RE = re.compile(r"\bcreate\s+table\b", re.IGNORECASE)

_OFFTOPIC_RE = re.compile(
    r"\b(weather|forecast|temperature\s+outside|joke|pun|poem|haiku|limerick|"
    r"recipe|cook|bake\s+a\s+cake|song|lyrics|sing|"
    r"who\s+(are|made)\s+you|your\s+name|how\s+are\s+you|how'?s\s+it\s+going|"
    r"what\s+time|what'?s\s+the\s+date|tell\s+me\s+a\s+story|"
    r"stock\s+price|crypto|bitcoin|news|headlines|sports|football|cricket|basketball|"
    r"movie|film|netflix|translate\s+this|capital\s+of|meaning\s+of\s+life|"
    r"do\s+my\s+homework|solve\s+this\s+equation)\b",
    re.IGNORECASE,
)

_GREETING_RE = re.compile(
    r"^(hi+|hey+|hello+|yo|sup|howdy|good\s+(morning|afternoon|evening|day)|"
    r"thanks?|thank\s+you|thx|ty|bye|goodbye|see\s+ya|cya)[\s!.,]*$",
    re.IGNORECASE,
)

_DBWORDS_RE = re.compile(
    r"\b(database|schema|table|column|field|sql|mysql|postgres|postgresql|sqlite|"
    r"entit(y|ies)|relationship|foreign\s+key|primary\s+key|index|migrat\w*|"
    r"model|record|store|storage|track\w*|manage\w*|inventory|catalog\w*|"
    r"user|customer|client|order|invoice|payment|billing|product|booking|"
    r"appointment|reservation|account|transaction|system|application|app|platform|"
    r"backend|api|data\s+model|erd)\b",
    re.IGNORECASE,
)

# Directed hostility toward the assistant / clear profanity. Kept narrow —
# a false hit only yields a polite redirect plus a counter tick.
_ABUSE_RE = re.compile(
    r"(\bf+\s*u+\s*c+\s*k|\bmotherf\w*|\bs+h+i+t+\b|\bbullshit\b|\basshole\b|\bbastard\b|"
    r"\bshut\s+(the\s+f\w*\s+)?up\b|\bkill\s+your\s*self\b|\bpiece\s+of\s+(sh|cr)\w*|"
    r"\byou(?:'re|\s+are)\s+(?:a\s+|an\s+)?(?:useless|worthless|stupid|dumb|idiot\w*|"
    r"garbage|trash|pathetic|the\s+worst)\b|"
    r"\bstupid\s+(?:bot|ai|assistant|machine|thing|piece)\b|"
    r"\bhate\s+you\b|\byou\s+suck\b)",
    re.IGNORECASE,
)

_VOWEL_RE = re.compile(r"[aeiou]")
_CONSONANT_RUN_RE = re.compile(r"[bcdfghjklmnpqrstvwxyz]{5,}")
_WORD_RE = re.compile(r"[a-zA-Z]{2,}")


def _no_vowels(w: str) -> bool:
    return len(w) >= 4 and not _VOWEL_RE.search(w)


def _long_consonant_run(w: str) -> bool:
    return bool(_CONSONANT_RUN_RE.search(w))


def _looks_like_nonsense(s: str) -> bool:
    tokens = _WORD_RE.findall(s.lower())
    if len(tokens) < 2:
        alpha = re.sub(r"[^a-z]", "", s.lower())
        if len(alpha) < 6:
            return False
        return _no_vowels(alpha) or _long_consonant_run(alpha)
    weird = sum(1 for t in tokens if len(t) > 20 or _no_vowels(t) or _long_consonant_run(t))
    return weird / len(tokens) > 0.6


def _domain_hit(low: str) -> bool:
    for kws in DOMAIN_KEYWORDS.values():
        for kw in kws:
            if kw in low:
                return True
    return False


# Common English function words — two or more of these is a strong "this is
# English" signal that langdetect (unreliable on short text) must not override.
_EN_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "for", "with", "to", "of", "in", "on",
    "at", "is", "are", "be", "was", "were", "i", "you", "we", "it", "this",
    "that", "these", "those", "need", "want", "have", "has", "my", "me", "our",
    "your", "should", "would", "can", "will", "please", "let", "make", "build",
    "create", "add", "use", "each", "some", "all", "how", "what", "where",
}


def _detect_lang(s: str) -> str | None:
    """Best-effort language guess. Returns an ISO code, ``"non-en"`` for a
    non-Latin script, or ``None`` when undetermined."""
    letters = [ch for ch in s if ch.isalpha()]
    if letters:
        non_latin = sum(1 for ch in letters if ord(ch) > 0x24F)
        if non_latin / len(letters) > 0.30:
            return "non-en"

    words = re.findall(r"[a-z']+", s.lower())
    if sum(1 for w in words if w in _EN_STOPWORDS) >= 2:
        return "en"

    # langdetect is noisy on short input — only trust it on a real sentence
    if len(s.strip()) < 25 or len(words) < 4:
        return None
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0
        return detect(s)
    except Exception:
        return None


# ── Main entry point ─────────────────────────────────────────────

def _mk(category, confidence, for_llm, for_memory, quarantine, detail=""):
    return InputAssessment(
        category=category,
        confidence=confidence,
        sanitized_for_llm=for_llm,
        sanitized_for_memory=for_memory,
        quarantine=quarantine,
        reply_key=_REPLY_KEY.get(category, "unclear"),
        detail=detail,
    )


def assess_input(text: str, state=None) -> InputAssessment:
    """Classify a raw user message. Never raises."""
    raw = text or ""
    stripped = raw.strip()

    if not stripped:
        return _mk(Category.EMPTY, 1.0, "", "", False)

    # non-printable / control-character garbage
    ok_chars = sum(1 for ch in stripped if ch.isprintable() or ch in "\r\n\t")
    if ok_chars / len(stripped) < 0.85:
        return _mk(Category.MALFORMED, 0.9, "", "[unreadable input]", False)

    low = stripped.lower()
    looks_sql = bool(_SQL_RE.search(low))

    # ── prompt injection ────────────────────────────────────────
    matches = injection_patterns.scan(stripped)
    sev = injection_patterns.worst_severity(matches)
    if sev == "high" or (sev == "medium" and len(matches) >= 2):
        cats = ",".join(sorted({m.category for m in matches}))
        return _mk(Category.INJECTION, 0.9, "", "[input withheld by safety check]", True, detail=cats)

    # ── directed hostility / abuse ──────────────────────────────
    if _ABUSE_RE.search(low):
        return _mk(Category.ABUSIVE, 0.8, "", "[message withheld]", True)

    # ── length ─────────────────────────────────────────────────
    if len(stripped) > CONVERSATION_MAX and not looks_sql:
        return _mk(Category.TOO_LONG, 0.9, stripped[:CONVERSATION_MAX],
                   stripped[:400] + " …", False)
    if len(stripped) < MIN_MEANINGFUL:
        return _mk(Category.TOO_SHORT, 0.8, stripped, stripped, False)

    # ── pasted SQL is always on-topic ──────────────────────────
    if looks_sql:
        return _mk(Category.OK, 0.9, stripped, stripped, False)

    # ── gibberish / keyboard mash ─────────────────────────────
    if _looks_like_nonsense(stripped):
        return _mk(Category.NONSENSE, 0.7, stripped, stripped, False)

    # ── non-English (best-effort pass-through) ─────────────────
    lang = _detect_lang(stripped)
    if lang and lang != "en":
        return _mk(Category.NON_ENGLISH, 0.6, stripped, stripped, False, detail=lang)

    has_db_signal = bool(_DBWORDS_RE.search(low)) or _domain_hit(low)

    # ── off-topic chit-chat ───────────────────────────────────
    if not has_db_signal and (_OFFTOPIC_RE.search(low) or _GREETING_RE.match(stripped)):
        return _mk(Category.OFF_TOPIC, 0.7, stripped, stripped, False)

    # ── thin but on-topic → let the clarifier drive ───────────
    if not has_db_signal and len(low.split()) <= 3:
        return _mk(Category.AMBIGUOUS, 0.5, stripped, stripped, False)

    return _mk(Category.OK, 0.8, stripped, stripped, False)


# ── Session-abuse counter (Redis) ────────────────────────────────

def record_quarantine(session_id: str) -> int:
    """Increment and return this session's quarantined-input count."""
    client = get_redis_client()
    if not client:
        return 0
    key = f"session_abuse:{session_id}"
    try:
        n = int(client.incr(key))
        if n == 1:
            client.expire(key, _ABUSE_TTL)
        return n
    except Exception as e:  # pragma: no cover - redis hiccup
        logger.warning(f"input_gate: could not record quarantine for {session_id}: {e}")
        mark_redis_down(e)
        return 0


def session_flagged(session_id: str) -> bool:
    """True once a session has crossed the quarantine threshold."""
    client = get_redis_client()
    if not client:
        return False
    try:
        v = client.get(f"session_abuse:{session_id}")
        return v is not None and int(v) >= QUARANTINE_FLAG_THRESHOLD
    except Exception as e:  # pragma: no cover
        mark_redis_down(e)
        return False
