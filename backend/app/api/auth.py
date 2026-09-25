"""认证接口:注册、登录、查询当前用户、修改密码。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core.deps import client_key, get_current_user
from ..core.security import create_access_token, hash_password, verify_password
from ..db import get_session
from ..models import User, UserRole
from ..schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from ..services.cache import get_cache

router = APIRouter(prefix="/api/auth", tags=["认证"])


def _rate_limit_or_raise(request: Request, key: str, limit: int, scope: str) -> None:
    """登录/注册类接口的限流,防止暴力破解。"""
    if not settings.rate_limit_enabled:
        return
    allowed, remaining = get_cache().rate_limit(key, limit, window_seconds=60)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"{scope}过于频繁,请稍后再试",
            headers={"Retry-After": "60"},
        )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED,
             summary="注册新用户")
async def register(
    payload: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    _rate_limit_or_raise(request, client_key(request), 10, "注册")

    exists = (
        await session.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已被占用")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        # 注册一律是普通用户,管理员只能由种子脚本创建
        role=UserRole.USER,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse, summary="登录获取令牌")
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    _rate_limit_or_raise(request, client_key(request), 20, "登录")

    user = (
        await session.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()

    # 用户不存在与密码错误返回同一提示,避免用户名枚举
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        )

    token = create_access_token(user.id, extra={"username": user.username, "role": user.role.value})
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut, summary="获取当前登录用户")
async def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post("/change-password", summary="修改密码")
async def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="当前密码不正确"
        )
    if payload.old_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="新密码不能与当前密码相同"
        )

    # 重新取一次,确保操作的是当前会话里的持久化对象
    db_user = await session.get(User, user.id)
    if db_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    db_user.password_hash = hash_password(payload.new_password)
    await session.commit()
    return {"message": "密码修改成功,请使用新密码重新登录"}
