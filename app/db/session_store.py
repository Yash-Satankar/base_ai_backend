import pickle
import logging
from typing import Optional
from app.core.config import settings

logger = logging.getLogger(__name__)

_redis_client = None


def get_redis_client():
    global _redis_client
    if _redis_client is None:
        try:
            import redis
            _redis_client = redis.Redis.from_url(
                settings.REDIS_URL,
                decode_responses=False,     # we store binary (pickle)
                socket_timeout=5,
                socket_connect_timeout=5,
                retry_on_timeout=True,
            )
            # Test connection
            _redis_client.ping()
            logger.info("✅ Redis connected")
        except Exception as e:
            if not settings.DEBUG:
                logger.critical(f"❌ Redis connection failed in production: {e}")
                raise RuntimeError(f"Redis connection failed in production: {e}")
            logger.warning(
                f"⚠️ Redis unavailable — falling back to memory store: {e}"
            )
            _redis_client = None
    return _redis_client


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
                pickle.dumps(state),
            )
            return True
        except Exception as e:
            logger.error(f"Redis save failed: {e}")
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
                return pickle.loads(data)
            return None
        except Exception as e:
            logger.error(f"Redis load failed: {e}")
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
            if not settings.DEBUG:
                raise RuntimeError(f"Redis TTL extend failed in production: {e}")
    return False