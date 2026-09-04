from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Optional


class Settings(BaseSettings):
    # App
    APP_NAME: str = "AI DB Schema Generator"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # AI Provider
    AI_PROVIDER: str = "groq"                  # "groq" | "together" | "anthropic" | "ollama"
    GROQ_API_KEY: Optional[str] = None         # required only when AI_PROVIDER=groq
    # Groq's current free-tier lineup (the llama-3.x ids were decommissioned in 2025).
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    # Together AI — OpenAI-compatible REST. Hosts the same gpt-oss lineup, no
    # free-tier TPM squeeze. Key lives in .env only.
    TOGETHER_API_KEY: Optional[str] = None
    TOGETHER_MODEL: str = "openai/gpt-oss-120b"
    TOGETHER_BASE_URL: str = "https://api.together.xyz/v1"
    # gpt-oss-120b can take >120s for a dense multi-table batch under load;
    # a longer read timeout keeps calls on the larger model instead of
    # bouncing to the 20b fallback.
    TOGETHER_TIMEOUT_SECONDS: int = 240
    # gpt-oss reasoning tokens count against max_tokens — at "medium"/default a
    # dense batch prompt can burn the whole budget on reasoning and emit no SQL.
    # "low" zeroes reasoning overhead; structured DDL/JSON output is unaffected.
    TOGETHER_REASONING_EFFORT: str = "low"    # "low" | "medium" | "high" | "" (off)
    ANTHROPIC_API_KEY: Optional[str] = None
    ANTHROPIC_MODEL: str = "claude-3-5-sonnet-20241022"

    # Ollama (local, no API key). Set AI_PROVIDER=ollama to use it.
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.1"
    OLLAMA_NUM_CTX: int = 16384               # context window — must fit the big system prompts

    # Generation
    MAX_TOKENS: int = 12000
    TOP_K_RULES: int = 15
    AI_TIMEOUT_SECONDS: int = 120
    # Longest a single Groq call will sleep out a transient (TPM) rate limit
    # before giving up on that model. Kept short so live API latency stays
    # bounded; batch/back-office runs raise it to pace to the free-tier window.
    GROQ_MAX_RATELIMIT_SLEEP: float = 8.0

    # Conversational loop (Phase 2)
    # Soft cost ceiling per conversation. There is NO hard stop — once a
    # conversation crosses this, clarifying rounds silently switch to the
    # cheaper model and push toward the blueprint sooner. The user never
    # sees anything about it; a single WARNING is logged for review.
    CONVERSATION_COST_WARN_USD: float = 0.05
    DEGRADE_MODEL: str = "openai/gpt-oss-20b"
    LLM_CACHE_TTL_SECONDS: int = 3600
    CONTEXT_COMPACT_AFTER_TURNS: int = 12    # fold older turns into a rolling summary past this

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

    # ── MySQL execution validation (optional post-validator gate) ──
    # Runs the generated DDL against a REAL MySQL 8 after SchemaValidator.
    # Off by default: a real DB spin-up is slow relative to a request.
    # See app/services/mysql_execution_validator.py.
    MYSQL_EXEC_VALIDATION_ENABLED: bool = False
    # Point at an existing MySQL 8 server (a scratch database is created and
    # dropped per run). Accepts mysql://user:pass@host:port/ or
    # mysql+pymysql://... . When unset, an ephemeral testcontainers MySQL is
    # used if Docker is available; otherwise the gate is skipped (not failed).
    MYSQL_EXEC_VALIDATION_DSN: Optional[str] = None
    MYSQL_EXEC_VALIDATION_USE_TESTCONTAINER: bool = True
    MYSQL_EXEC_VALIDATION_TIMEOUT: int = 90

    # ── Auto-iteration schema refinement (post-generation stage) ──
    # After initial generation, loop schema_validator + mysql_execution_validator
    # and feed the concrete findings (real MySQL errors + enterprise check text)
    # back to the LLM for a targeted fix, up to N iterations. Off by default —
    # it costs an LLM call per iteration and (when exec validation is on) a MySQL
    # round-trip. Honours the Decision-B cost degrade: a degraded conversation is
    # capped to a single iteration. See app/services/schema_refiner.py.
    SCHEMA_REFINE_ENABLED: bool = False
    SCHEMA_REFINE_MAX_ITERATIONS: int = 3
    # A schema is "clean" when it has no structural critical/high issues, MySQL
    # accepts it, no enterprise-check errors, and advisory findings are at or
    # below this count.
    SCHEMA_REFINE_ADVISORY_THRESHOLD: int = 5
    # The refiner returns the whole corrected schema each iteration, so the
    # budget must hold a full mid-size schema (~180 tok/table) plus headroom.
    SCHEMA_REFINE_MAX_TOKENS: int = 16000

    # Completeness gate for the async generation job. After per-batch retries are
    # exhausted, if (distinct tables generated / tables planned) falls below this
    # ratio the job FAILS — a structurally-clean fragment must never be handed
    # back as a finished schema. See generate_database_schema_for_job.
    SCHEMA_COMPLETENESS_MIN_RATIO: float = 0.85

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