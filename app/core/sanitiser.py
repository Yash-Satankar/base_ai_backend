# app/core/sanitiser.py

import re
from fastapi import HTTPException


INJECTION_PATTERNS = [
    r'ignore\s+(all\s+)?previous\s+instructions',
    r'system\s+prompt',
    r'you\s+are\s+now',
    r'disregard\s+',
    r'forget\s+your\s+instructions',
    r'act\s+as\s+',
    r'pretend\s+you\s+are',
    r'jailbreak',
    r'dan\s+mode',
    r'drop\s+table',
    r'delete\s+from',
    r'truncate\s+table',
    r'--\s*$',
    r';\s*drop',
    r'<script',
    r'javascript:',
    r'on\w+\s*=',        # onclick=, onload=, etc.
]

MAX_MESSAGE_LENGTH = 5000
MIN_MESSAGE_LENGTH = 2


def sanitise_user_input(text: str) -> str:
    """
    Clean and validate user input.
    Raises HTTPException if input is malicious.
    Returns cleaned text.
    """
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    if len(text) > MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Message too long. Maximum {MAX_MESSAGE_LENGTH} characters."
        )

    if len(text.strip()) < MIN_MESSAGE_LENGTH:
        raise HTTPException(
            status_code=400,
            detail="Message too short."
        )

    # Check for prompt injection
    text_lower = text.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            raise HTTPException(
                status_code=400,
                detail="Invalid input detected."
            )

    # Strip HTML tags
    text = re.sub(r'<[^>]+>', '', text)

    # Normalise whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    return text