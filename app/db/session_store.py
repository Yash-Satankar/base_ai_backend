import time
import logging
from typing import Optional
from app.core.config import settings
from app.db import session_codec

logger = logging.getLogger(__name__)

_redis_client = None

# Negative-cache: after a failed connection probe, stop re-probing (each probe
# is a multi-second socket timeout) until this monotonic deadline passes.
# Without this, a down Redis makes every call in a conversational turn stall
# for seconds — a turn touches Redis 5-10 times.
_redis_down_until: float = 0.0
_REDIS_RETRY_BACKOFF_SECONDS = 30.0


def mark_redis_down(exc: Exception | None = None) -> None:
    """Drop the cached client and open a backoff window. Call this when a Redis
    *operation* (not just the connect probe) fails mid-session."""
    global _redis_client, _redis_down_until
    _redis_client = None
    _redis_down_until = time.monotonic() + _REDIS_RETRY_BACKOFF_SECONDS
    if exc is not None:
        logger.warning(f"⚠️ Redis marked down for {_REDIS_RETRY_BACKOFF_SECONDS:.0f}s: {exc}")


def get_redis_client():
    global _redis_client, _redis_down_until

    if _redis_client is not None:
        return _redis_client

    # Inside the backoff window — do NOT re-probe (that costs a socket timeout).
    if time.monotonic() < _redis_down_until:
        if not settings.DEBUG:
            raise RuntimeError("Redis is unavailable (in backoff window).")
        return None

    try:
        import redis
        client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=False,     # we store bytes (see session_codec)
            socket_timeout=5,
            socket_connect_timeout=2,   # fail fast on connect
            retry_on_timeout=False,
            health_check_interval=30,
        )
        client.ping()
        _redis_client = client
        _redis_down_until = 0.0
        logger.info("✅ Redis connected")
        return _redis_client
    except Exception as e:
        _redis_down_until = time.monotonic() + _REDIS_RETRY_BACKOFF_SECONDS
        if not settings.DEBUG:
            logger.critical(f"❌ Redis connection failed in production: {e}")
            raise RuntimeError(f"Redis connection failed in production: {e}")
        logger.warning(
            f"⚠️ Redis unavailable — memory fallback, not re-probing for "
            f"{_REDIS_RETRY_BACKOFF_SECONDS:.0f}s: {e}"
        )
        return None


def _reset_redis_state() -> None:
    """Test helper — clear the cached client and backoff window."""
    global _redis_client, _redis_down_until
    _redis_client = None
    _redis_down_until = 0.0


# ── In-memory fallback ───────────────────────────────────────────
# Used when Redis is not available (local dev without Redis)
_memory_store: dict = {}


def save_session(state) -> bool:
    """Save session state. Returns True on success."""
    session_key = f"session:{state.session_id}"

    client = get_redis_client()
    if client:
        try:
            client.setex(
                session_key,
                settings.REDIS_SESSION_TTL,
                session_codec.encode(state),
            )
            return True
        except Exception as e:
            logger.error(f"Redis save failed: {e}")
            mark_redis_down(e)
            if not settings.DEBUG:
                raise RuntimeError(f"Redis save failed in production: {e}")

    if not settings.DEBUG:
        raise RuntimeError("Redis client is not available in production.")

    # Memory fallback
    _memory_store[state.session_id] = state
    return True


def load_session(session_id: str) -> Optional[object]:
    """Load session state. Returns None if not found."""
    session_key = f"session:{session_id}"

    client = get_redis_client()
    if client:
        try:
            data = client.get(session_key)
            if data:
                return session_codec.decode(data)
            return None
        except Exception as e:
            logger.error(f"Redis load failed: {e}")
            mark_redis_down(e)
            if not settings.DEBUG:
                raise RuntimeError(f"Redis load failed in production: {e}")

    if not settings.DEBUG:
        raise RuntimeError("Redis client is not available in production.")

    # Memory fallback
    return _memory_store.get(session_id)


def delete_session(session_id: str) -> bool:
    """Delete a session."""
    session_key = f"session:{session_id}"

    client = get_redis_client()
    if client:
        try:
            client.delete(session_key)
            return True
        except Exception as e:
            logger.error(f"Redis delete failed: {e}")
            mark_redis_down(e)
            if not settings.DEBUG:
                raise RuntimeError(f"Redis delete failed in production: {e}")

    if not settings.DEBUG:
        raise RuntimeError("Redis client is not available in production.")

    # Memory fallback
    _memory_store.pop(session_id, None)
    return True


def session_exists(session_id: str) -> bool:
    """Check if session exists."""
    return load_session(session_id) is not None


def extend_session_ttl(session_id: str) -> bool:
    """Extend session TTL on activity."""
    session_key = f"session:{session_id}"
    client = get_redis_client()
    if client:
        try:
            client.expire(session_key, settings.REDIS_SESSION_TTL)
            return True
        except Exception as e:
            logger.error(f"Redis TTL extend failed: {e}")
            mark_redis_down(e)
            if not settings.DEBUG:
                raise RuntimeError(f"Redis TTL extend failed in production: {e}")
    return False