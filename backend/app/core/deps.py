"""FastAPI 依赖:当前用户解析、角色校验。"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import User, UserRole
from .security import decode_access_token

# auto_error=False:自己控制未带 token 时的错误信息与状态码
bearer_scheme = HTTPBearer(auto_error=False)

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="登录状态无效或已过期,请重新登录",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """解析 Bearer token 并返回对应用户。"""
    if credentials is None or not credentials.credentials:
        raise CREDENTIALS_ERROR

    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise CREDENTIALS_ERROR

    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError):
        raise CREDENTIALS_ERROR

    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise CREDENTIALS_ERROR
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """仅允许管理员。普通用户访问管理类接口一律 403。"""
    if user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return user


def client_key(request: Request, user: User | None = None) -> str:
    """限流用的标识:优先按用户,其次按 IP。"""
    if user is not None:
        return f"user:{user.id}"
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "unknown"
    )
    return f"ip:{ip}"
