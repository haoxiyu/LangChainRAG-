"""密码哈希与 JWT 签发/校验。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from ..config import settings

# bcrypt 算法本身只处理前 72 字节,超出部分会被静默丢弃。
# 这里显式拒绝超长密码,避免"两个不同长密码哈希相同"的隐患。
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    """用 bcrypt 生成密码哈希。"""
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError(f"密码过长(最多 {MAX_PASSWORD_BYTES} 字节)")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码是否匹配哈希。"""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # 哈希串损坏或格式非法时一律视为校验失败,不抛异常
        return False


def create_access_token(subject: str | int, extra: dict[str, Any] | None = None) -> str:
    """签发 JWT。subject 存用户 id,extra 可放 role 等声明。"""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """解析 JWT,失败(过期/篡改/格式错)返回 None。"""
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except jwt.PyJWTError:
        return None
