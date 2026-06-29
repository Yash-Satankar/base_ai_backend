import re
import logging
from fastapi import Security, HTTPException, status, Depends, Request
from fastapi.security import APIKeyHeader
from slowapi import Limiter
from slowapi.util import get_remote_address
from app.core.config import settings

logger = logging.getLogger(__name__)

# ── Rate limiter ─────────────────────────────────────────────────
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200/day", "50/hour"],
)

# ── API key auth ─────────────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    api_key: str = Security(api_key_header),
) -> str:
    """
    Verify the API key in the X-API-Key header.
    Returns the key if valid, raises 401/403 if not.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Add X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if api_key != settings.MASTER_API_KEY:
        logger.warning(f"Invalid API key attempt: {api_key[:8]}...")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )

    return api_key


# ── Input sanitisation ───────────────────────────────────────────

MAX_MESSAGE_LENGTH = 50000
MIN_MESSAGE_LENGTH = 2

INJECTION_PATTERNS = [
    r'ignore\s+(all\s+)?previous\s+instructions',
    r'disregard\s+(all\s+)?instructions',
    r'you\s+are\s+now\s+(a|an)',
    r'act\s+as\s+(a|an|if)',
    r'pretend\s+(you\s+are|to\s+be)',
    r'forget\s+(your\s+)?(instructions|rules|guidelines)',
    r'new\s+instructions?\s*:',
    r'system\s*:\s*you',
    r'jailbreak',
    r'\[system\]',
    r'\[instructions?\]',
    # SQL injection
    r';\s*(drop|delete|truncate|alter)\s+table',
    r'union\s+select',
    r'--\s*drop',
    r'xp_cmdshell',
    # XSS
    r'<script[^>]*>',
    r'javascript\s*:',
    r'on(?:click|load|error|focus)\s*=',
]

COMPILED_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS
]


def sanitise_input(text: str) -> str:
    """
    Validate and clean user input.
    Raises HTTPException on malicious input.
    Returns cleaned text.
    """
    if not text or not text.strip():
        raise HTTPException(
            status_code=400,
            detail="Message cannot be empty.",
        )

    text = text.strip()

    if len(text) < MIN_MESSAGE_LENGTH:
        raise HTTPException(
            status_code=400,
            detail="Message too short.",
        )

    if len(text) > MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Message too long. Max {MAX_MESSAGE_LENGTH} characters.",
        )

    # Check injection patterns
    for pattern in COMPILED_PATTERNS:
        if pattern.search(text):
            logger.warning(
                f"Injection attempt blocked: {text[:100]}"
            )
            raise HTTPException(
                status_code=400,
                detail="Invalid input detected.",
            )

    # Strip HTML tags
    text = re.sub(r'<[^>]+>', '', text)

    # Normalise whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    return text


def sanitise_project_name(name: str) -> str:
    """Clean project name for use in filenames."""
    name = re.sub(r'[^\w\s-]', '', name)
    name = re.sub(r'\s+', '_', name.strip())
    return name[:50] or "project"