"""会话管理接口。

安全要点:所有查询都必须带上 user_id 过滤。
只按会话 id 查询会让用户通过改 id 读到别人的对话(水平越权)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import get_current_user
from ..db import get_session
from ..models import Conversation, Message, User
from ..schemas.chat import (
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationPage,
    ConversationUpdate,
    MessageOut,
)

router = APIRouter(prefix="/api/conversations", tags=["会话"])


async def _get_own_conversation(
    session: AsyncSession, conversation_id: int, user: User
) -> Conversation:
    """取出属于当前用户的会话;不存在或不属于该用户都返回 404。

    这里刻意不区分"不存在"与"无权访问",避免泄露他人会话 id 是否存在。
    """
    conv = (
        await session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return conv


@router.get("", response_model=ConversationPage, summary="我的会话列表")
async def list_conversations(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    keyword: str | None = Query(None, description="按标题搜索"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> ConversationPage:
    conditions = [Conversation.user_id == user.id]
    if keyword:
        conditions.append(Conversation.title.ilike(f"%{keyword.strip()}%"))

    total = (
        await session.execute(
            select(func.count(Conversation.id)).where(*conditions)
        )
    ).scalar_one()

    stmt = (
        select(Conversation)
        .where(*conditions)
        .order_by(Conversation.updated_at.desc())
        .offset(offset)
        .limit(limit)
    )
    convs = (await session.execute(stmt)).scalars().all()
    if not convs:
        return ConversationPage(items=[], total=total)

    # 一次性统计消息数,避免逐个会话查询
    ids = [c.id for c in convs]
    counts = dict(
        (
            await session.execute(
                select(Message.conversation_id, func.count(Message.id))
                .where(Message.conversation_id.in_(ids))
                .group_by(Message.conversation_id)
            )
        ).all()
    )

    out: list[ConversationOut] = []
    for c in convs:
        item = ConversationOut.model_validate(c)
        item.message_count = counts.get(c.id, 0)
        out.append(item)
    return ConversationPage(items=out, total=total)


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED,
             summary="新建会话")
async def create_conversation(
    payload: ConversationCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ConversationOut:
    conv = Conversation(
        user_id=user.id,
        title=payload.title.strip() or "新对话",
    )
    session.add(conv)
    await session.commit()
    await session.refresh(conv)

    out = ConversationOut.model_validate(conv)
    out.message_count = 0
    return out


@router.get("/{conversation_id}", response_model=ConversationDetail, summary="会话详情")
async def get_conversation(
    conversation_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    message_limit: int = Query(100, ge=1, le=500),
) -> ConversationDetail:
    conv = await _get_own_conversation(session, conversation_id, user)

    # 从新往旧取 N 条再反转:直接正序 limit 拿到的是最早的消息,
    # 长会话下最新几轮反而看不到。
    msgs = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.id.desc())
            .limit(message_limit)
        )
    ).scalars().all()
    msgs.reverse()

    c_out = ConversationOut.model_validate(conv)
    c_out.message_count = len(msgs)
    return ConversationDetail(
        conversation=c_out,
        messages=[MessageOut.model_validate(m) for m in msgs],
    )


@router.get("/{conversation_id}/messages", response_model=list[MessageOut],
            summary="会话消息(游标分页,用于上拉加载历史)")
async def list_messages(
    conversation_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    before_id: int | None = Query(None, description="返回 id 小于该值的消息,用于向上翻页"),
    limit: int = Query(50, ge=1, le=200),
) -> list[MessageOut]:
    conv = await _get_own_conversation(session, conversation_id, user)

    stmt = select(Message).where(Message.conversation_id == conv.id)
    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)
    # 先按 id 倒序取最近的 N 条,再翻回正序返回给前端
    stmt = stmt.order_by(Message.id.desc()).limit(limit)

    msgs = list((await session.execute(stmt)).scalars().all())
    msgs.reverse()
    return [MessageOut.model_validate(m) for m in msgs]


@router.patch("/{conversation_id}", response_model=ConversationOut,
              summary="更新会话(重命名)")
async def update_conversation(
    conversation_id: int,
    payload: ConversationUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ConversationOut:
    conv = await _get_own_conversation(session, conversation_id, user)

    # exclude_unset 才能区分「没传」与「显式传了 null」
    changes = payload.model_dump(exclude_unset=True)

    if "title" in changes:
        title = (changes["title"] or "").strip()
        if not title:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="标题不能为空"
            )
        conv.title = title

    await session.commit()
    await session.refresh(conv)

    count = (
        await session.execute(
            select(func.count(Message.id)).where(Message.conversation_id == conv.id)
        )
    ).scalar_one()
    out = ConversationOut.model_validate(conv)
    out.message_count = count
    return out


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="删除会话")
async def delete_conversation(
    conversation_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> None:
    conv = await _get_own_conversation(session, conversation_id, user)
    await session.delete(conv)
    await session.commit()


@router.delete("", status_code=status.HTTP_200_OK, summary="清空我的全部会话")
async def clear_conversations(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, int]:
    convs = (
        await session.execute(select(Conversation).where(Conversation.user_id == user.id))
    ).scalars().all()
    for c in convs:
        await session.delete(c)
    await session.commit()
    return {"deleted": len(convs)}
