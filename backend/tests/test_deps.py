"""鉴权依赖的单元测试:令牌解析、角色校验、限流标识。

这三段是需求 6「仅管理员可进知识库管理」在服务端的唯一落点:
- get_current_user 一旦把坏令牌当合法,任何人都能顶着别人的身份取数据;
- require_admin 一旦漏判,普通用户就能删知识库、删文档。

前端隐藏菜单只是体验,这里才是真正的门,所以单独覆盖。
不连数据库:execute 用最小 stub 顶替,只验证「拿到用户之后怎么判」。
"""

from __future__ import annotations

from typing import Any

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.config import settings
from app.core.deps import client_key, get_current_user, require_admin
from app.core.security import create_access_token
from app.models import User, UserRole

# 项目没装 pytest-asyncio,异步用例靠 anyio 自带的 pytest 插件跑。
# 模块级打一次标记即可,同文件里的同步用例不受影响。
pytestmark = pytest.mark.anyio


# ---------- 测试替身 ----------
def _user(role: UserRole = UserRole.USER, uid: int = 1) -> User:
    """构造一个不落库的 User 对象:ORM 模型允许直接实例化。"""
    return User(id=uid, username=f"u{uid}", password_hash="x", role=role)


class _Result:
    """模拟 SQLAlchemy Result 里本项目用到的那一个方法。"""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """只实现 get_current_user 用到的一次 execute。"""

    def __init__(self, found: User | None) -> None:
        self.found = found

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self.found)


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _request(headers: dict[str, str] | None = None,
             client: tuple[str, int] | None = ("1.2.3.4", 1234)) -> Request:
    """直接构造 Starlette Request,避免起测试服务器。"""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
    }
    if client is not None:
        scope["client"] = client
    return Request(scope)


# ---------- 令牌解析 ----------
async def test_valid_token_resolves_user() -> None:
    token = create_access_token(7)
    user = await get_current_user(_creds(token), _FakeSession(_user(uid=7)))
    assert user.id == 7


async def test_missing_credentials_is_401() -> None:
    with pytest.raises(HTTPException) as err:
        await get_current_user(None, _FakeSession(_user()))
    assert err.value.status_code == 401


async def test_empty_credentials_is_401() -> None:
    """Authorization 头写成 "Bearer " 时 credentials 是空串,不能当成合法令牌。"""
    with pytest.raises(HTTPException) as err:
        await get_current_user(_creds(""), _FakeSession(_user()))
    assert err.value.status_code == 401


async def test_garbage_token_is_401() -> None:
    with pytest.raises(HTTPException) as err:
        await get_current_user(_creds("not.a.jwt"), _FakeSession(_user()))
    assert err.value.status_code == 401


async def test_token_with_other_secret_is_401() -> None:
    """用别人的密钥签的令牌必须被拒 —— 否则等于没有鉴权。"""
    # 密钥取 32 字节以上,免得 PyJWT 额外抛 InsecureKeyLengthWarning 盖住真正的断言
    forged = jwt.encode(
        {"sub": "1"}, "attacker-secret-that-is-long-enough-x", algorithm=settings.algorithm
    )
    with pytest.raises(HTTPException) as err:
        await get_current_user(_creds(forged), _FakeSession(_user()))
    assert err.value.status_code == 401


async def test_token_with_non_numeric_subject_is_401() -> None:
    """sub 不是数字时 int() 会抛 ValueError,必须转成 401 而不是 500。"""
    bad = jwt.encode({"sub": "admin"}, settings.secret_key, algorithm=settings.algorithm)
    with pytest.raises(HTTPException) as err:
        await get_current_user(_creds(bad), _FakeSession(_user()))
    assert err.value.status_code == 401


async def test_token_of_deleted_user_is_401() -> None:
    """令牌签名有效但用户已被删除:不能放行,也不能抛未捕获异常。"""
    token = create_access_token(999)
    with pytest.raises(HTTPException) as err:
        await get_current_user(_creds(token), _FakeSession(None))
    assert err.value.status_code == 401


# ---------- 角色校验 ----------
async def test_require_admin_passes_admin_through() -> None:
    admin = _user(UserRole.ADMIN)
    assert await require_admin(admin) is admin


async def test_require_admin_rejects_normal_user_with_403() -> None:
    with pytest.raises(HTTPException) as err:
        await require_admin(_user(UserRole.USER))
    assert err.value.status_code == 403
    assert "管理员" in err.value.detail


# ---------- 限流标识 ----------
def test_client_key_prefers_user_id() -> None:
    assert client_key(_request(), _user(uid=42)) == "user:42"


def test_client_key_falls_back_to_ip() -> None:
    """未登录接口(注册/登录)也要限流,此时只能按 IP 计数。"""
    assert client_key(_request()) == "ip:1.2.3.4"


def test_client_key_uses_first_forwarded_ip() -> None:
    """经过反向代理时 request.client 是代理地址,必须取 X-Forwarded-For 第一段。"""
    req = _request({"X-Forwarded-For": "9.9.9.9, 10.0.0.1"})
    assert client_key(req) == "ip:9.9.9.9"


def test_client_key_without_client_info() -> None:
    """测试客户端等场景下没有 client,不能因此抛异常。"""
    assert client_key(_request(client=None)) == "ip:unknown"
