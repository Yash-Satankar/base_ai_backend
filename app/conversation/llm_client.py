# app/conversation/llm_client.py
"""
The single tagged entry point for every conversational LLM call.

Responsibilities:
  - MANDATORY operation tagging  — `operation` is keyword-only with no default,
    so a call that forgets to name itself fails loudly.
  - Cost accounting — per-conversation (Redis, survives workers) and per-turn
    (contextvar), priced per model.
  - Warn-and-degrade (Decision B) — once a conversation crosses the soft cost
    threshold, `should_degrade()` returns True and callers pass `degrade=True`
    to route to the cheaper model. There is no hard stop and nothing about it
    is ever surfaced to the user; a single WARNING is logged.
  - Response caching for classifier-style calls (`cache=True`) keyed by a hash
    of (operation, model, prompts).
"""

import json
import time
import hashlib
import logging
import contextvars

from app.core.config import settings
from app.core.telemetry import TelemetryManager
from app.services.ai_service import generate_schema
from app.db.session_store import get_redis_client, mark_redis_down

logger = logging.getLogger(__name__)

# ── Per-model rates ($ per 1M tokens: input, output) ──────────────
_MODEL_RATES = {
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant":    (0.05, 0.08),
    "qwen/qwen3.6-27b":        (0.29, 0.39),
    "openai/gpt-oss-120b":     (0.59, 0.79),
    "_default":                (0.59, 0.79),
}

_COST_TTL = settings.REDIS_SESSION_TTL
_turn_cost: contextvars.ContextVar[float] = contextvars.ContextVar("_turn_cost", default=0.0)

# Ambient conversation attribution — lets detached workers (the blueprint
# job thread) attribute their LLM cost to a conversation without threading
# session_id through every function signature.
_ctx_session: contextvars.ContextVar = contextvars.ContextVar("_ctx_session", default=None)
_ctx_project: contextvars.ContextVar = contextvars.ContextVar("_ctx_project", default=None)


def set_context(session_id: str = None, project_id: str = None) -> None:
    _ctx_session.set(session_id)
    _ctx_project.set(project_id)


def clear_context() -> None:
    _ctx_session.set(None)
    _ctx_project.set(None)

# in-memory fallbacks (used when Redis is unavailable, e.g. local dev)
_mem_conv_cost: dict[str, float] = {}
_mem_warned: set[str] = set()
_mem_cache: dict[str, dict] = {}


def _price(model: str, usage: dict) -> float:
    rin, rout = _MODEL_RATES.get(model, _MODEL_RATES["_default"])
    cost = (usage.get("input_tokens", 0) / 1_000_000) * rin \
         + (usage.get("output_tokens", 0) / 1_000_000) * rout
    return round(cost, 6)


# ── Per-turn accounting ──────────────────────────────────────────
def reset_turn_cost() -> None:
    _turn_cost.set(0.0)


def turn_cost() -> float:
    return round(_turn_cost.get(), 6)


# ── Per-conversation accounting ─────────────────────────────────
def _add_conversation_cost(session_id: str, amount: float) -> float:
    _turn_cost.set(_turn_cost.get() + amount)
    if not session_id:
        return amount
    client = get_redis_client()
    key = f"conv_cost:{session_id}"
    if client:
        try:
            total = client.incrbyfloat(key, amount)
            client.expire(key, _COST_TTL)
            return float(total)
        except Exception as e:  # pragma: no cover
            logger.warning(f"llm_client: conv cost accounting failed for {session_id}: {e}")
            mark_redis_down(e)
    _mem_conv_cost[session_id] = _mem_conv_cost.get(session_id, 0.0) + amount
    return _mem_conv_cost[session_id]


def conversation_cost(session_id: str) -> float:
    if not session_id:
        return 0.0
    client = get_redis_client()
    if client:
        try:
            v = client.get(f"conv_cost:{session_id}")
            return float(v) if v is not None else 0.0
        except Exception as e:  # pragma: no cover
            mark_redis_down(e)
    return _mem_conv_cost.get(session_id, 0.0)


def should_degrade(session_id: str) -> bool:
    """True once a conversation has crossed the soft cost threshold."""
    if not session_id:
        return False
    return conversation_cost(session_id) >= settings.CONVERSATION_COST_WARN_USD


def _warn_once(session_id: str, total: float) -> None:
    if not session_id:
        return
    client = get_redis_client()
    flag = f"conv_cost_warned:{session_id}"
    first = False
    if client:
        try:
            first = bool(client.set(flag, "1", nx=True, ex=_COST_TTL))
        except Exception as e:  # pragma: no cover
            mark_redis_down(e)
            first = session_id not in _mem_warned
            _mem_warned.add(session_id)
    else:
        first = session_id not in _mem_warned
        _mem_warned.add(session_id)
    if first:
        logger.warning(
            f"💸 Conversation {session_id} crossed the ${settings.CONVERSATION_COST_WARN_USD:.2f} "
            f"soft cost threshold (now ${total:.4f}) — clarifying rounds will degrade to the "
            f"cheaper model. No user-facing change."
        )


# ── Response cache (classifier-style calls) ─────────────────────
def _cache_key(operation: str, model: str, system_prompt: str, user_prompt: str) -> str:
    h = hashlib.sha256(f"{operation}|{model}|{system_prompt}|{user_prompt}".encode("utf-8")).hexdigest()
    return f"llm_cache:{h}"


def _cache_get(key: str):
    client = get_redis_client()
    if client:
        try:
            raw = client.get(key)
            if raw:
                return json.loads(raw)
        except Exception as e:  # pragma: no cover
            mark_redis_down(e)
    return _mem_cache.get(key)


def _cache_put(key: str, value: dict) -> None:
    client = get_redis_client()
    if client:
        try:
            client.setex(key, settings.LLM_CACHE_TTL_SECONDS, json.dumps(value))
            return
        except Exception as e:  # pragma: no cover
            mark_redis_down(e)
    _mem_cache[key] = value


# ── Main entry point ────────────────────────────────────────────
def call_llm(
    *,
    operation: str,
    system_prompt: str,
    user_prompt: str,
    session_id: str = None,
    project_id: str = None,
    max_tokens: int = None,
    model: str = None,
    temperature: float = None,
    cache: bool = False,
    degrade: bool = False,
) -> dict:
    """
    Make one tagged LLM call. Returns generate_schema()'s dict plus
    ``operation``, ``cost_usd``, ``degraded`` and ``cached``.
    """
    if not operation:
        raise ValueError("call_llm requires a non-empty `operation` tag")

    # fall back to ambient conversation attribution (blueprint job thread, etc.)
    session_id = session_id or _ctx_session.get(None)
    project_id = project_id or _ctx_project.get(None)

    # cost-degrade: forced by caller, or automatic once over threshold
    auto_degrade = degrade or should_degrade(session_id)
    effective_model = model or (settings.DEGRADE_MODEL if auto_degrade else None)

    key = None
    if cache:
        key = _cache_key(operation, effective_model or "", system_prompt, user_prompt)
        hit = _cache_get(key)
        if hit is not None:
            return {**hit, "operation": operation, "cost_usd": 0.0,
                    "degraded": auto_degrade, "cached": True}

    start = time.time()
    resp = generate_schema(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
        model=effective_model,
        temperature=temperature,
    )
    duration = time.time() - start

    used_model = resp.get("model", effective_model or settings.GROQ_MODEL)
    usage = resp.get("usage", {})
    cost = _price(used_model, usage)

    total = _add_conversation_cost(session_id, cost)
    if session_id and total >= settings.CONVERSATION_COST_WARN_USD:
        _warn_once(session_id, total)

    TelemetryManager.log_operation(
        operation=operation,
        duration_sec=duration,
        tokens=usage,
        model=used_model,
        success=True,
        estimated_cost_usd=cost,
        conversation_id=session_id,
        project_id=project_id,
    )

    out = {**resp, "operation": operation, "cost_usd": cost,
           "degraded": auto_degrade, "cached": False}

    if cache and key:
        _cache_put(key, {"content": resp.get("content", ""), "usage": usage, "model": used_model})

    return out
