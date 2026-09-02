# app/core/debug_gate.py
"""
Gate for the internal 'debug' response projection.

The default API contract is deliberately lean — it must read like one
assistant, not a pipeline, so it never ships L1-L7 metadata, rule IDs,
validator internals, or provider/model names. This dependency decides
whether a given request is allowed to receive that internal detail.

Access requires BOTH:
  1. an explicit opt-in header  ``X-Debug: true``   (never a query param)
  2. a staff caller:
       - authenticated with the master API key, or
       - a user whose email is listed in ``DEBUG_VIEW_EMAILS``

Anything else — anonymous callers, regular users, a bare query string —
gets the lean response.
"""

import logging
from typing import Optional

from fastapi import Depends, Header

from app.core.auth import get_current_user_optional
from app.core.config import settings
from app.db.models import User

logger = logging.getLogger(__name__)


async def require_debug_view(
    x_debug: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
    current_user: Optional[User] = Depends(get_current_user_optional),
) -> bool:
    """Return True only for an explicit, authenticated staff debug request."""
    if not x_debug or x_debug.strip().lower() != "true":
        return False

    # Master API key => system/staff caller.
    if settings.MASTER_API_KEY and x_api_key and x_api_key == settings.MASTER_API_KEY:
        return True

    allow = settings.get_debug_view_emails()
    if current_user is not None and (current_user.email or "").lower() in allow:
        return True

    logger.info(
        "Debug view requested without staff credentials — serving lean response."
    )
    return False
