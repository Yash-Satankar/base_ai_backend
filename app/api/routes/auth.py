# app/api/routes/auth.py
"""
Authentication router.
Provides endpoints for registering, logging in, and managing developer API keys.
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.repositories.user_repo import UserRepository
from app.schemas.auth_schemas import (
    UserRegisterRequest, UserLoginRequest, AuthResponse,
    CreateApiKeyRequest, CreateApiKeyResponse, ApiKeyResponse
)
from app.core.auth import create_access_token, get_current_user
from app.db.models import User

router = APIRouter()


@router.post("/register", response_model=dict, status_code=status.HTTP_201_CREATED)
async def register(body: UserRegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new developer account."""
    user_repo = UserRepository(db)
    existing_user = await user_repo.get_by_email(body.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email address already exists."
        )
    
    user = await user_repo.create(
        email=body.email,
        display_name=body.display_name,
        password=body.password
    )
    # Save transaction
    await db.commit()

    return {
        "success": True,
        "message": "User registered successfully.",
        "user_id": user.id,
        "email": user.email
    }


@router.post("/login", response_model=AuthResponse)
async def login(body: UserLoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate and obtain a session JWT token."""
    user_repo = UserRepository(db)
    user = await user_repo.get_by_email(body.email)
    if not user or not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password."
        )

    is_valid = user_repo.verify_password(body.password, user.hashed_password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password."
        )

    token = create_access_token(user_id=user.id, email=user.email)
    return AuthResponse(
        success=True,
        access_token=token,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name
    )


@router.post("/api-keys", response_model=CreateApiKeyResponse)
async def create_api_key(
    body: CreateApiKeyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Generate a new developer API key."""
    user_repo = UserRepository(db)
    api_key_obj, raw_key = await user_repo.create_api_key(
        user_id=current_user.id,
        name=body.name,
        expires_days=body.expires_days
    )
    await db.commit()

    return CreateApiKeyResponse(
        success=True,
        api_key_details=ApiKeyResponse(
            id=api_key_obj.id,
            name=api_key_obj.name,
            key_prefix=api_key_obj.key_prefix,
            created_at=api_key_obj.created_at,
            expires_at=api_key_obj.expires_at,
            last_used_at=api_key_obj.last_used_at,
            is_active=api_key_obj.is_active
        ),
        raw_api_key=raw_key
    )


@router.get("/api-keys", response_model=List[ApiKeyResponse])
async def list_api_keys(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """List all active developer API keys."""
    user_repo = UserRepository(db)
    keys = await user_repo.list_api_keys(current_user.id)
    return [
        ApiKeyResponse(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            created_at=k.created_at,
            expires_at=k.expires_at,
            last_used_at=k.last_used_at,
            is_active=k.is_active
        )
        for k in keys
    ]


@router.delete("/api-keys/{key_id}", response_model=dict)
async def revoke_api_key(
    key_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Revoke and deactivate a developer API key."""
    user_repo = UserRepository(db)
    succeeded = await user_repo.revoke_api_key(current_user.id, key_id)
    if not succeeded:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found or already revoked."
        )
    await db.commit()
    return {"success": True, "message": "API key revoked successfully."}
