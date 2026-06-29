# app/core/auth.py
"""
Authentication and authorization middleware.
Handles JWT token issuance, verification, and unified HTTP bearer / API key authentication.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional
import jwt
from fastapi import Depends, HTTPException, status, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.db.models import User
from app.db.repositories.user_repo import UserRepository

# Security schemes
security_jwt = HTTPBearer(auto_error=False)
security_api_key = APIKeyHeader(name="X-API-Key", auto_error=False)


def create_access_token(user_id: str, email: str) -> str:
    """Generate a JWT access token for a user session."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.TOKEN_EXPIRE_MINUTES)
    to_encode = {
        "sub": user_id,
        "email": email,
        "exp": expire
    }
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt


def verify_access_token(token: str) -> Optional[dict]:
    """Decode and verify access token. Returns payload dict or None."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_jwt),
    api_key: Optional[str] = Security(security_api_key),
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Unified authentication dependency.
    Checks for HTTP Bearer JWT token first, then falls back to X-API-Key header verification.
    """
    user_repo = UserRepository(db)

    # 1. Try JWT Auth
    if credentials and credentials.scheme.lower() == "bearer":
        payload = verify_access_token(credentials.credentials)
        if payload:
            user_id = payload.get("sub")
            if user_id:
                user = await user_repo.get_by_id(user_id)
                if user:
                    return user

    # 2. Try API Key Auth
    if api_key:
        # Check against master key for local development / internal system scripts
        if settings.MASTER_API_KEY and api_key == settings.MASTER_API_KEY:
            # Return system-level placeholder user
            system_user = await user_repo.get_by_email("system@baseai.dev")
            if not system_user:
                # Lazy-create system placeholder user on first master key usage
                system_user = await user_repo.create(
                    email="system@baseai.dev",
                    display_name="System Administrator",
                    password=secrets_token()  # random password
                )
                await db.commit()
            return system_user

        # Otherwise check project-specific user API keys
        user = await user_repo.verify_api_key(api_key)
        if user:
            return user

    # Unauthorized
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials. Include a valid Bearer JWT token or X-API-Key header.",
        headers={"WWW-Authenticate": "Bearer, ApiKey"},
    )


def secrets_token() -> str:
    import secrets
    return secrets.token_urlsafe(32)


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_jwt),
    api_key: Optional[str] = Security(security_api_key),
    db: AsyncSession = Depends(get_db)
) -> Optional[User]:
    """
    Optional authentication helper.
    Returns User if token or key is valid, otherwise returns None (no error raised).
    """
    try:
        return await get_current_user(credentials, api_key, db)
    except HTTPException:
        return None