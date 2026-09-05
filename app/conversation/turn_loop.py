# app/conversation/turn_loop.py
"""
The lean per-turn loop: Observe → Think → Act → Verify.

`run_turn` replaces the old intent-detect + stage-routing block inside
`process_message`. It:
  - OBSERVE: classify (done upstream by the input gate), detect domain ONCE
    per turn and cache it, compact the transcript if it has grown, detect intent
  - THINK:   for INITIAL/CLARIFYING, one structured clarify call (merged from
    the old two); other stages delegate to the existing handlers
  - ACT:     advance the stage, update working memory, or hand the L1-L8
    blueprint compile to an async job
VERIFY (leak/structure guard) stays in `process_message._finalize`.
"""

import asyncio
import hashlib
import logging
import re

from app.engine.conversation_engine import ConversationStage, run_clarify_turn
from app.engine.intent_detector import detect_intent, IntentType
from app.engine.decomposition_signals import detect_decomposition_signal
from app.conversation import context_builder, llm_client
from app.prompts import persona
from app.guardrails.input_gate import Category
from app.core.config import settings

logger = logging.getLogger(__name__)

_META_WORDS = {"status", "where are we", "what's happening", "whats happening", "progress", "summary"}

# ── Schema decomposition confirmation (docs/enterprise_standards_spec.md §2.2) ──
_DECOMPOSE_STRONG_YES = re.compile(r"\bseparate\s+schemas?\b|\bsplit\b|\bmultiple\s+schemas?\b", re.IGNORECASE)
_DECOMPOSE_STRONG_NO = re.compile(r"\bunified\b|\bone\s+schema\b|\bsingle\s+schema\b", re.IGNORECASE)


def _parse_decomposition_answer(msg_lower: str) -> bool:
    """Defaults to False (single schema) on an ambiguous or empty answer —
    the conservative choice: no source supports decomposing without a clear
    signal, so an unclear reply must not silently trigger it."""
    if _DECOMPOSE_STRONG_YES.search(msg_lower):
        return True
    if _DECOMPOSE_STRONG_NO.search(msg_lower):
        return False
    return bool(re.search(r"\byes\b", msg_lower))


_DECOMPOSITION_QUESTION = (
    "One thing before I put the blueprint together: this sounds like it might "
    "span more than one independent business domain.\n\n"
    "Would you like me to design it as **separate schemas** (one per domain — "
    "useful if different teams will own them independently), or as **one "
    "unified schema**? Reply with either option."
)


def _h(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()[:12]


def _domain_once(state, text: str):
    """detect_domain / detect_all_domains at most once per (turn, text)."""
    from app.engine.rule_matcher import detect_domain, detect_all_domains

    key = _h(text)
    if state.facts.get("_domain") and state.facts.get("_domain_for") == key:
        return state.facts["_domain"], state.facts.get("_all_domains", [state.facts["_domain"]])

    domain, conf = detect_domain(text)
    all_domains = detect_all_domains(text)
    state.facts["_domain"] = domain
    state.facts["_domain_for"] = key
    state.facts["_all_domains"] = all_domains
    state.facts["_domain_conf"] = conf
    return domain, all_domains


def _language_ack(state, assessment) -> str | None:
    """Decision C — a one-shot in-persona language acknowledgement, woven into
    the first Think call and never repeated."""
    if assessment is None or assessment.category != Category.NON_ENGLISH:
        return None
    if state.facts.get("_lang_ack_done"):
        return None
    state.facts["_lang_ack_done"] = True
    lang = (assessment.detail or "").strip() or "another language"
    return persona.NON_ENGLISH_GUIDANCE.format(language=lang)


async def run_turn(state, user_message: str, assessment) -> dict:
    import app.services.conversation_service as cs

    # ── OBSERVE ────────────────────────────────────────────────
    llm_client.reset_turn_cost()

    basis = user_message if state.stage == ConversationStage.INITIAL else (state.requirement_summary or user_message)
    domain, all_domains = _domain_once(state, basis)

    # fold older turns into the rolling summary if the transcript has grown
    try:
        await asyncio.to_thread(context_builder.maybe_compact, state)
    except Exception as e:  # pragma: no cover
        logger.warning(f"turn_loop: compaction error ignored ({e})")

    intent = detect_intent(user_message, state)
    logger.info(f"🎯 Intent: {intent.type} ({intent.confidence}) | Stage: {state.stage} | turn ${llm_client.turn_cost():.4f}")

    # ── THINK + ACT ───────────────────────────────────────────
    # cross-stage intents first (unchanged handlers)
    if intent.type == IntentType.START_OVER:
        return cs.handle_start_over(state)
    if intent.type == IntentType.AMBIGUOUS and state.stage not in (
        ConversationStage.INITIAL, ConversationStage.CLARIFYING
    ):
        return cs.handle_ambiguous(state, user_message, intent)
    if intent.type == IntentType.CONTEXT_SWITCH:
        return cs.handle_context_switch(state, user_message)
    if intent.type == IntentType.PASTE_SQL:
        return cs.handle_paste_sql(state, user_message)
    if intent.type == IntentType.EXPLAIN:
        return cs.handle_explain(state, user_message, intent)
    if intent.type == IntentType.DOWNLOAD:
        return cs.handle_download_request(state, intent)
    if intent.type == IntentType.REGENERATE:
        resp = cs.handle_regenerate(state)
        return cs._handle_generation(state) if resp.get("ready_to_generate") else resp
    if user_message.lower().strip() in _META_WORDS:
        return cs.handle_session_summary(state)

    # stage-specific
    if state.stage == ConversationStage.INITIAL:
        return await _think_initial(state, user_message, domain, all_domains, assessment)

    if state.stage == ConversationStage.CLARIFYING:
        return await _think_clarifying(state, user_message, domain, assessment)

    if state.stage == ConversationStage.COMPILING:
        return {
            "message": "I'm still putting the blueprint together — one moment. "
                       "It'll appear here as soon as it's ready.",
            "stage": state.stage,
            "session_id": state.session_id,
        }

    if state.stage == ConversationStage.BLUEPRINT:
        if intent.type == IntentType.CONFIRM:
            return cs._handle_blueprint_confirmation(state, "yes")
        if intent.type == IntentType.CONFIRM_WITH_CHANGE:
            return cs.handle_confirm_with_change(state, user_message, intent)
        if intent.type in (IntentType.EDIT, IntentType.ADD, IntentType.REMOVE):
            state.requirement_summary += f"\n\nUser modification: {user_message}"
            return cs._handle_blueprint_confirmation(state, user_message)
        return cs._handle_blueprint_confirmation(state, user_message)

    if state.stage == ConversationStage.CONFIRMED:
        return cs._handle_generation(state)

    if state.stage == ConversationStage.GENERATING:
        if intent.type == IntentType.REGENERATE:
            state.schema = None
            state.validation_score = None
            state.fix_attempts = 0
            state.sql_file_path = None
            state.pdf_file_path = None
            state.stage = ConversationStage.CONFIRMED
            return cs._handle_generation(state)
        return {
            "message": "I'm still generating your schema — hang tight, it'll show up here "
                       "when it's done. If it seems stuck, say **retry** and I'll start fresh.",
            "stage": state.stage,
            "session_id": state.session_id,
        }

    if state.stage == ConversationStage.COMPLETE:
        if intent.type == IntentType.CONFIRM:
            return cs.handle_download_request(state, intent)
        if intent.type == IntentType.QUESTION:
            return cs.handle_question(state, user_message, intent)
        return {
            "message": "Your schema is ready.\n\n"
                       "- **download** to get your SQL and PDF\n"
                       "- **explain [table name]** to walk through a table\n"
                       "- **start over** to build something new",
            "stage": state.stage,
            "session_id": state.session_id,
        }

    return {
        "message": "Let's pick this back up — tell me what you'd like to do next.",
        "stage": state.stage,
        "session_id": state.session_id,
    }


# ── Think: INITIAL / CLARIFYING (one merged call) ───────────────

async def _think_initial(state, user_message, domain, all_domains, assessment) -> dict:
    import app.services.conversation_service as cs

    state.requirement_summary = user_message
    state.stage = ConversationStage.CLARIFYING

    ack = _language_ack(state, assessment)
    degrade = llm_client.should_degrade(state.session_id)
    transcript = context_builder.transcript_for_prompt(state)

    clar = await asyncio.to_thread(
        run_clarify_turn, state, domain, transcript,
        round_number=1, degrade=degrade, language_ack=ack,
    )

    questions = clar.get("questions", [])
    state.questions_asked.extend(q.get("question", "") for q in questions)
    state.understood_aspects = clar.get("understood", {}) or {}

    domain_label = domain.replace("_", " ").title()
    understood = clar.get("understood_so_far", "")
    message = (
        f"I can see this is a **{domain_label}** project.\n\n"
        f"Here's what I've got so far:\n_{understood}_\n\n"
        f"A few questions to sharpen the design:\n\n"
        f"{cs._format_questions(questions)}\n\n"
        f"Answer what's relevant — and say **Generate Blueprint** whenever you're ready."
    )
    return {
        "message": message,
        "stage": state.stage,
        "detected_domain": domain,
        "all_domains": all_domains,
        "clarification_round": 1,
        "understanding_confidence": clar.get("confidence", 0),
        "session_id": state.session_id,
    }


async def _think_clarifying(state, user_message, domain, assessment) -> dict:
    import app.services.conversation_service as cs

    msg_lower = user_message.lower().strip()

    # A pending decomposition confirmation (asked last turn) takes priority
    # over everything else this turn — the user was asked a direct question.
    if (settings.SCHEMA_DECOMPOSITION_ENABLED and state.decomposition_question_asked
            and state.decomposition_requested is None):
        state.decomposition_requested = _parse_decomposition_answer(msg_lower)
        logger.info(f"🧩 Decomposition confirmed: {state.decomposition_requested}")
        return cs._blueprint_job_trigger(state)

    wants_to_proceed = any(sig in msg_lower for sig in cs.GENERATE_BLUEPRINT_SIGNALS)

    if user_message.strip():
        tag = "Additional context" if state.clarifications_done == 0 else f"Round {state.clarifications_done + 1} answers"
        state.requirement_summary += f"\n\n{tag}:\n{user_message}"
    state.clarifications_done += 1

    if wants_to_proceed:
        # Ask, at most once, before compiling — never decide silently. Off
        # unless SCHEMA_DECOMPOSITION_ENABLED (default False): every existing
        # deployment and test keeps today's single-schema behavior untouched.
        if (settings.SCHEMA_DECOMPOSITION_ENABLED and not state.decomposition_question_asked
                and detect_decomposition_signal(state.requirement_summary)):
            state.decomposition_question_asked = True
            logger.info("🧩 Decomposition signal detected — asking for confirmation")
            return {
                "message": _DECOMPOSITION_QUESTION,
                "stage": state.stage,
                "session_id": state.session_id,
            }
        logger.info(f"✅ User triggered blueprint after {state.clarifications_done} rounds")
        return cs._blueprint_job_trigger(state)

    ack = _language_ack(state, assessment)
    degrade = llm_client.should_degrade(state.session_id)
    transcript = context_builder.transcript_for_prompt(state)

    clar = await asyncio.to_thread(
        run_clarify_turn, state, domain, transcript,
        round_number=state.clarifications_done + 1, degrade=degrade, language_ack=ack,
    )
    state.understood_aspects = clar.get("understood", {}) or state.understood_aspects

    # Decision B: once degraded, push toward the blueprint sooner
    if degrade and clar.get("ready_for_blueprint"):
        logger.info("💸 Degraded + ready_for_blueprint → proceeding to blueprint compile")
        return cs._blueprint_job_trigger(state)

    questions = clar.get("questions", [])
    state.questions_asked.extend(q.get("question", "") for q in questions)

    one_line = clar.get("one_line_summary", "your project")
    confidence = clar.get("confidence", 50)
    gaps = clar.get("key_gaps", [])
    gaps_text = f"\n\n*Still firming up: {', '.join(gaps[:3])}*" if gaps else ""
    round_num = state.clarifications_done + 1

    if confidence >= 85:
        tail = "\n\nI have a clear picture now — add more detail if you like, or say **Generate Blueprint**."
    else:
        tail = "\n\nAnswer what you can, or say **Generate Blueprint** when you feel I've got it."

    message = (
        f"**Where I'm at:** _{one_line}_\n"
        f"{cs._confidence_bar(confidence)}{gaps_text}\n\n"
        f"Next questions (Round {round_num}):\n\n"
        f"{cs._format_questions(questions)}{tail}"
    )
    return {
        "message": message,
        "stage": state.stage,
        "clarification_round": round_num,
        "understanding_confidence": confidence,
        "session_id": state.session_id,
    }
