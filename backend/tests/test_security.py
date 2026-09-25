"""密码哈希与 JWT 的单元测试。

这两块是鉴权的根:一个负责「密码不可逆、不可伪造」,一个负责「令牌不可篡改、
会过期」。任何一处回归都等于系统被人拿到 admin 权限,所以单独覆盖。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.config import settings
from app.core.security import (
    MAX_PASSWORD_BYTES,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


# ---------- 密码哈希 ----------
def test_hash_is_not_plaintext_and_verifies() -> None:
    pwd = "123456"
    hashed = hash_password(pwd)
    assert pwd not in hashed
    assert verify_password(pwd, hashed) is True


def test_wrong_password_rejected() -> None:
    hashed = hash_password("correct-horse")
    assert verify_password("wrong-horse", hashed) is False


def test_same_password_hashes_differently() -> None:
    """加盐:相同明文两次哈希必须不同,否则可以靠比对哈希猜出相同密码。"""
    assert hash_password("123456") != hash_password("123456")


def test_password_over_72_bytes_is_rejected() -> None:
    """bcrypt 只处理前 72 字节,不显式拒绝会导致超长密码互相等价。"""
    too_long = "a" * (MAX_PASSWORD_BYTES + 1)
    with pytest.raises(ValueError):
        hash_password(too_long)


def test_password_of_exactly_72_bytes_is_allowed() -> None:
    hashed = hash_password("a" * MAX_PASSWORD_BYTES)
    assert verify_password("a" * MAX_PASSWORD_BYTES, hashed) is True


def test_verify_returns_false_on_corrupt_hash() -> None:
    """库里哈希串损坏时应当返回 False,而不是把异常抛到接口层变成 500。"""
    assert verify_password("123456", "not-a-bcrypt-hash") is False
    assert verify_password("123456", "") is False


# ---------- JWT ----------
def test_token_roundtrip_carries_subject_and_extra() -> None:
    token = create_access_token(7, {"role": "admin"})
    payload = decode_access_token(token)
    assert payload is not None
    # sub 按 JWT 规范存成字符串,取用户时再转回 int
    assert payload["sub"] == "7"
    assert payload["role"] == "admin"


def test_token_signed_with_other_key_is_rejected() -> None:
    now = datetime.now(timezone.utc)
    forged = jwt.encode(
        {
            "sub": "1",
            "role": "admin",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        # 故意用另一把密钥伪造;长度取 32 字节以上,免得 PyJWT 额外报密钥过短
        "attacker-secret-that-is-long-enough-x",
        algorithm=settings.algorithm,
    )
    assert decode_access_token(forged) is None


def test_expired_token_is_rejected() -> None:
    now = datetime.now(timezone.utc)
    expired = jwt.encode(
        {
            "sub": "1",
            "iat": int((now - timedelta(hours=2)).timestamp()),
            "exp": int((now - timedelta(hours=1)).timestamp()),
        },
        settings.secret_key,
        algorithm=settings.algorithm,
    )
    assert decode_access_token(expired) is None


def test_garbage_token_is_rejected() -> None:
    assert decode_access_token("not.a.jwt") is None
    assert decode_access_token("") is None
