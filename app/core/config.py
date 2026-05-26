# app/core/config.py

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # App
    APP_NAME: str = "Base AI"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # ─── AI Provider ───────────────────────────────────────────
    # Switch between providers by changing AI_PROVIDER in .env
    # Options: "groq" | "anthropic"
    AI_PROVIDER: str = "groq"

    # Groq (free) — get key at console.groq.com
    GROQ_API_KEY: str = "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"
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    # Anthropic (paid) — uncomment when you have credits
    # ANTHROPIC_API_KEY: str = ""
    # ANTHROPIC_MODEL: str = "claude-sonnet-4-20250514"

    # Shared generation settings
    MAX_TOKENS: int = 8000
    TOP_K_RULES: int = 15
    # ───────────────────────────────────────────────────────────

    # Qdrant
    QDRANT_URL: str
    QDRANT_API_KEY: str
    QDRANT_COLLECTION_NAME: str = "db_rules"

    # Postgres
    DATABASE_URL: str

    # Embedding
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    EMBEDDING_DIMENSION: int = 384

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()