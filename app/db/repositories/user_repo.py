# app/db/repositories/user_repo.py
"""
Repository layer for Users and API Keys.
Handles CRUD and password/API key hashing using bcrypt.
"""

from typing import Optional, List
import bcrypt
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import User, ApiKey
import secrets


class UserRepository:
    """Handles persistence operations for Users and their ApiKeys."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── User Operations ──────────────────────────────────────────

    async def get_by_id(self, user_id: str) -> Optional[User]:
        result = await self.db.execute(select(User).where(User.id == user_id, User.is_active == True))
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Optional[User]:
        result = await self.db.execute(select(User).where(User.email == email.lower(), User.is_active == True))
        return result.scalar_one_or_none()

    async def create(self, email: str, display_name: str, password: str) -> User:
        # Hash password using bcrypt
        salt = bcrypt.gensalt()
        hashed_pw = bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

        user = User(
            email=email.lower(),
            display_name=display_name,
            hashed_password=hashed_pw,
            is_active=True,
            is_verified=False
        )
        self.db.add(user)
        await self.db.flush()
        return user

    def verify_password(self, password: str, hashed_password: str) -> bool:
        """Verify standard password against stored bcrypt hash."""
        try:
            return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))
        except Exception:
            return False

    # ── API Key Operations ────────────────────────────────────────

    async def create_api_key(self, user_id: str, name: str, expires_days: Optional[int] = None) -> tuple[ApiKey, str]:
        """
        Generate a cryptographically secure API key.
        Returns the ApiKey database object and the raw API key string (which must only be shown once!).
        """
        # Form: b_ai_live_[32 random chars]
        raw_token = f"b_ai_live_{secrets.token_hex(16)}"
        
        # We hash the key using bcrypt for one-way verification (Cursor/OpenAI model)
        salt = bcrypt.gensalt()
        hashed_key = bcrypt.hashpw(raw_token.encode("utf-8"), salt).decode("utf-8")

        expires_at = None
        if expires_days:
            expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)

        api_key_obj = ApiKey(
            user_id=user_id,
            name=name,
            key_prefix=raw_token[:12],  # first 12 characters to identify the key in API requests
            key_hash=hashed_key,
            expires_at=expires_at,
            is_active=True
        )

        self.db.add(api_key_obj)
        await self.db.flush()
        return api_key_obj, raw_token

    async def get_api_key_by_prefix(self, prefix: str) -> Optional[ApiKey]:
        result = await self.db.execute(
            select(ApiKey).where(ApiKey.key_prefix == prefix, ApiKey.is_active == True)
        )
        return result.scalar_one_or_none()

    async def verify_api_key(self, raw_key: str) -> Optional[User]:
        """
        Verify raw API key from header.
        Returns the User owner if valid, otherwise None.
        """
        if not raw_key or len(raw_key) < 12:
            return None

        prefix = raw_key[:12]
        api_key = await self.get_api_key_by_prefix(prefix)
        if not api_key:
            return None

        # Check expiration
        if api_key.expires_at and api_key.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            return None

        # Verify key hash
        is_valid = bcrypt.checkpw(raw_key.encode("utf-8"), api_key.key_hash.encode("utf-8"))
        if not is_valid:
            return None

        # Update last used timestamp (using execute to avoid loading entity blockages)
        await self.db.execute(
            update(ApiKey)
            .where(ApiKey.id == api_key.id)
            .values(last_used_at=datetime.now(timezone.utc))
        )
        
        # Load user
        return await self.get_by_id(api_key.user_id)

    async def list_api_keys(self, user_id: str) -> List[ApiKey]:
        result = await self.db.execute(
            select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.is_active == True)
        )
        return list(result.scalars().all())

    async def revoke_api_key(self, user_id: str, key_id: str) -> bool:
        result = await self.db.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.user_id == user_id)
            .values(is_active=False)
        )
        return result.rowcount > 0
