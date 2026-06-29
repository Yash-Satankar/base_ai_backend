# app/schemas/auth_schemas.py
"""
Pydantic schemas for authentication and API key management.
"""

from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field


class UserRegisterRequest(BaseModel):
    email: EmailStr = Field(..., description="Valid unique email address")
    display_name: str = Field(..., min_length=2, max_length=100, description="Display name for the user")
    password: str = Field(..., min_length=6, max_length=100, description="Password (min 6 characters)")


class UserLoginRequest(BaseModel):
    email: EmailStr = Field(..., description="User account email")
    password: str = Field(..., description="User account password")


class AuthResponse(BaseModel):
    success: bool
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    display_name: str


class CreateApiKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Friendly descriptive name for the key (e.g. CI/CD environment)")
    expires_days: Optional[int] = Field(None, ge=1, le=365, description="Optional key expiration length in days (max 365)")


class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    is_active: bool


class CreateApiKeyResponse(BaseModel):
    success: bool
    api_key_details: ApiKeyResponse
    raw_api_key: str = Field(..., description="The raw API key. MUST only be displayed once to the user.")
