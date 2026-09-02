# tests/conftest.py
"""
Shared test setup.

Sets dummy environment variables BEFORE anything under ``app`` is imported so
``app.core.config.Settings`` can construct without a real ``.env`` and without
tripping the production JWT-secret guard (DEBUG=true).

No test in this suite makes a real network call: Redis / Postgres / Qdrant /
Groq are never contacted — the relevant functions are monkeypatched or their
dependencies overridden per test.
"""

import os

os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("QDRANT_API_KEY", "test-qdrant-key")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/testdb"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("MASTER_API_KEY", "test-master-key")

import pytest


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """No test contacts a real Redis. Force the in-memory fallback everywhere
    (otherwise `get_redis_client()` re-attempts a 5s socket timeout on every
    call, since it does not cache the failure). Patch every module that
    imported the symbol by name."""
    for modname in (
        "app.db.session_store",
        "app.conversation.llm_client",
        "app.guardrails.input_gate",
        "app.services.job_store",
    ):
        try:
            import importlib
            mod = importlib.import_module(modname)
            if hasattr(mod, "get_redis_client"):
                monkeypatch.setattr(mod, "get_redis_client", lambda: None)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _reset_llm_client_state():
    """Phase 2: clear the in-memory cost/cache fallbacks between tests."""
    try:
        from app.conversation import llm_client
        llm_client._mem_conv_cost.clear()
        llm_client._mem_warned.clear()
        llm_client._mem_cache.clear()
        llm_client.reset_turn_cost()
        llm_client.clear_context()
    except Exception:
        pass
    yield
