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
