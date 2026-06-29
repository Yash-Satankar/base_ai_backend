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
    # ANTHROPIC_API_KEY: str = ""
    # ANTHROPIC_MODEL: str = "claude-sonnet-4-20250514"

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

    # Embedding
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    EMBEDDING_DIMENSION: int = 384

    def get_allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",")]

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()