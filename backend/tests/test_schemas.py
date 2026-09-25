"""请求/响应模型的单元测试。

其中 ConversationUpdate 的 exclude_unset 语义是重点:接口靠它区分「没传这个
字段」和「传了 null」。之前实现里 title 是必填,导致重命名之外的更新都写不进去;
改成可选后,这条语义就必须有回归用例守着。

会话已不再绑定知识库(检索跨全库),所以这里只锁重命名的语义。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.auth import ChangePasswordRequest, RegisterRequest
from app.schemas.chat import ChatRequest, ConversationCreate, ConversationUpdate, MessageOut
from app.schemas.knowledge import KnowledgeBaseCreate


# ---------- 局部更新的语义 ----------
def test_empty_patch_yields_no_changes() -> None:
    """什么都不传 = 什么都不改,不能顺手把标题写成 null。"""
    assert ConversationUpdate().model_dump(exclude_unset=True) == {}


def test_patch_title_only() -> None:
    payload = ConversationUpdate(title="新标题")
    assert payload.model_dump(exclude_unset=True) == {"title": "新标题"}


def test_patch_ignores_legacy_knowledge_base_field() -> None:
    """旧前端仍可能带上 knowledge_base_id,既不能报错也不能写进变更集。

    检索范围已与知识库无关,这个字段留着只会让人以为「选库还有用」。
    """
    payload = ConversationUpdate(title="改名", knowledge_base_id=3)
    assert payload.model_dump(exclude_unset=True) == {"title": "改名"}


def test_patch_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        ConversationUpdate(title="")


def test_patch_rejects_overlong_title() -> None:
    with pytest.raises(ValidationError):
        ConversationUpdate(title="字" * 129)


# ---------- 其它模型 ----------
def test_conversation_create_defaults_to_new_chat() -> None:
    assert ConversationCreate().title == "新对话"


def test_conversation_create_no_longer_accepts_knowledge_base() -> None:
    """新建会话不再需要选库:检索跨全库,多余的字段只会误导前端继续传。"""
    assert "knowledge_base_id" not in ConversationCreate.model_fields


def test_message_references_default_is_none() -> None:
    """历史用户消息本身没有引用,默认值必须是 None 而不是空列表 ——
    前端靠它区分「没有引用」与「引用为空」。"""
    assert MessageOut.model_fields["references"].default is None


def test_register_requires_min_length_username_and_password() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(username="ab", password="123456")
    with pytest.raises(ValidationError):
        RegisterRequest(username="student", password="12345")


def test_register_rejects_overlong_password() -> None:
    """与 bcrypt 的 72 字节上限呼应,接口层先挡住明显过长的输入。"""
    with pytest.raises(ValidationError):
        RegisterRequest(username="student", password="a" * 65)


def test_change_password_requires_new_password_length() -> None:
    with pytest.raises(ValidationError):
        ChangePasswordRequest(old_password="123456", new_password="123")


def test_chat_request_requires_non_empty_question() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(question="")
    with pytest.raises(ValidationError):
        ChatRequest(question="字" * 2001)
    assert ChatRequest(question="电池多大").question == "电池多大"


def test_knowledge_base_name_is_required() -> None:
    with pytest.raises(ValidationError):
        KnowledgeBaseCreate(name="")
    assert KnowledgeBaseCreate(name="商品库").description == ""
