"""认证相关的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.security import MAX_PASSWORD_BYTES


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, description="用户名")
    password: str = Field(min_length=6, max_length=64, description="密码")

    @field_validator("username")
    @classmethod
    def username_pattern(cls, v: str) -> str:
        v = v.strip()
        if not v.replace("_", "").isalnum():
            raise ValueError("用户名只能包含字母、数字和下划线")
        return v

    @field_validator("password")
    @classmethod
    def password_length(cls, v: str) -> str:
        # 与 bcrypt 的 72 字节上限对齐,提前拦截而不是让哈希静默截断
        if len(v.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError(f"密码过长(最多 {MAX_PASSWORD_BYTES} 字节)")
        return v


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(description="当前密码")
    new_password: str = Field(min_length=6, max_length=64, description="新密码")

    @field_validator("new_password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError(f"密码过长(最多 {MAX_PASSWORD_BYTES} 字节)")
        return v


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    created_at: datetime

    @field_validator("role", mode="before")
    @classmethod
    def role_to_str(cls, v):
        return v.value if hasattr(v, "value") else v


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
