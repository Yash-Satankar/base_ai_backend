# app/conversation/context_builder.py
"""
Assembles the conversational context handed to each LLM call, and keeps it
bounded.

Instead of naively growing the prompt every turn, older turns are folded
into a rolling summary once the transcript passes a threshold, and the
commitments the user made are pulled out into `state.key_decisions`. The
last few turns are always kept verbatim.

`transcript_for_prompt()` replaces the old
`ConversationState.get_conversation_so_far()`.
"""

import json
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

KEEP_VERBATIM = 8            # most-recent messages always kept word-for-word
_TURN_CHARS = 500           # per-message clip in the verbatim tail

# messages the input gate stored in place of withheld content — never feed these back
_WITHHELD = {
    "[input withheld by safety check]",
    "[message withheld]",
    "[unreadable input]",
    "[input removed by safety filter]",
}


def _visible(messages):
    return [m for m in messages if (m.content or "").strip() not in _WITHHELD]


def transcript_for_prompt(state) -> str:
    """Rolling summary + key decisions + the verbatim recent tail."""
    parts = []

    if state.rolling_summary:
        parts.append(f"[Summary of the earlier conversation]\n{state.rolling_summary}")

    if state.key_decisions:
        parts.append(
            "[Decisions the user has made]\n"
            + "\n".join(f"- {d}" for d in state.key_decisions[-12:])
        )

    tail = _visible(state.messages)[-KEEP_VERBATIM:]
    if tail:
        lines = []
        for m in tail:
            who = "User" if m.role == "user" else "Assistant"
            lines.append(f"{who}: {(m.content or '')[:_TURN_CHARS]}")
        parts.append("[Recent turns]\n" + "\n".join(lines))

    return "\n\n".join(parts) if parts else "(no conversation yet)"


def build(state) -> dict:
    return {
        "transcript": transcript_for_prompt(state),
        "rolling_summary": state.rolling_summary,
        "key_decisions": list(state.key_decisions),
        "facts": dict(state.facts),
    }


_COMPACT_SYSTEM = """You compress the earlier part of a database-design conversation.
Return ONLY valid JSON:
{
  "summary": "tight paragraph (<=120 words) covering what the project is, who uses it, the core entities/workflows, scale, and any compliance needs stated so far",
  "decisions": ["short, concrete commitments the user has made or confirmed"]
}
Keep it factual. Do not invent details that were not stated."""


def maybe_compact(state) -> bool:
    """
    If the transcript has outgrown the threshold, fold the older turns into
    `state.rolling_summary` and pull commitments into `state.key_decisions`.
    One cheap LLM call, gated on turn count. Returns True if it compacted.
    """
    visible = _visible(state.messages)
    summarized_upto = int(state.facts.get("_summarized_upto", 0))
    pending = len(visible) - summarized_upto

    if pending <= settings.CONTEXT_COMPACT_AFTER_TURNS:
        return False

    cutoff = len(visible) - KEEP_VERBATIM
    to_fold = visible[summarized_upto:cutoff]
    if not to_fold:
        return False

    block = "\n".join(
        f"{'User' if m.role == 'user' else 'Assistant'}: {(m.content or '')[:_TURN_CHARS]}"
        for m in to_fold
    )
    prior = f"Existing summary:\n{state.rolling_summary}\n\n" if state.rolling_summary else ""

    try:
        from app.conversation.llm_client import call_llm
        resp = call_llm(
            operation="context_compact",
            system_prompt=_COMPACT_SYSTEM,
            user_prompt=f"{prior}New turns to fold in:\n{block}",
            session_id=state.session_id,
            project_id=state.project_id,
            degrade=True,          # compaction is never worth the premium model
            max_tokens=500,
        )
        content = (resp.get("content") or "").strip()
        if "```" in content:
            for p in content.split("```"):
                p = p.strip()
                if p.startswith("json"):
                    p = p[4:].strip()
                if p.startswith("{"):
                    content = p
                    break
        data = json.loads(content)
    except Exception as e:
        logger.warning(f"context_builder: compaction skipped ({e})")
        return False

    new_summary = (data.get("summary") or "").strip()
    if new_summary:
        state.rolling_summary = new_summary

    for d in data.get("decisions", []):
        d = (d or "").strip()
        if d and d not in state.key_decisions:
            state.key_decisions.append(d)

    state.facts["_summarized_upto"] = cutoff
    logger.info(
        f"🗜️ Compacted {len(to_fold)} turns into the rolling summary "
        f"(session {state.session_id}, {len(state.key_decisions)} decisions tracked)"
    )
    return True
