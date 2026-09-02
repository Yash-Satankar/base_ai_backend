from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Optional


class Settings(BaseSettings):
    # App
    APP_NAME: str = "AI DB Schema Generator"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # AI Provider
    AI_PROVIDER: str = "groq"
    GROQ_API_KEY: str                          # no default — must be set in .env
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    ANTHROPIC_API_KEY: Optional[str] = None
    ANTHROPIC_MODEL: str = "claude-3-5-sonnet-20241022"

    # Generation
    MAX_TOKENS: int = 12000
    TOP_K_RULES: int = 15
    AI_TIMEOUT_SECONDS: int = 120

    # Qdrant
    QDRANT_URL: str
    QDRANT_API_KEY: str
    QDRANT_COLLECTION_NAME: str = "db_rules"

    # Postgres
    DATABASE_URL: str

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_SESSION_TTL: int = 86400      # 24 hours

    # Security
    MASTER_API_KEY: str = ""
    ALLOWED_ORIGINS: str = "http://localhost:3000"
    JWT_SECRET: str = "supersecretjwtkeyforlocaldevenvironmentchangeinproduction"
    JWT_ALGORITHM: str = "HS256"
    TOKEN_EXPIRE_MINUTES: int = 1440  # 24 hours

    # Debug response projection — comma-separated staff emails allowed to
    # request the internal `X-Debug: true` view (L1-L7 metadata, rule IDs,
    # provider/model names). Empty by default → only the master API key qualifies.
    DEBUG_VIEW_EMAILS: str = ""

    # Embedding
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    EMBEDDING_DIMENSION: int = 384

    def get_allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",")]

    def get_debug_view_emails(self) -> set[str]:
        return {
            e.strip().lower()
            for e in self.DEBUG_VIEW_EMAILS.split(",")
            if e.strip()
        }

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    if not s.DEBUG and s.JWT_SECRET == "supersecretjwtkeyforlocaldevenvironmentchangeinproduction":
        raise ValueError(
            "SECURITY FAILURE: The default JWT_SECRET cannot be used in production. "
            "Please configure a secure JWT_SECRET in your .env file."
        )
    return s


settings = get_settings()