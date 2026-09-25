"""问答接口:SSE 流式返回,边生成边推送引用片段。

事件协议(每条都是 `data: {json}\\n\\n`):
  conversation —— 会话 id(新建会话时前端据此更新侧栏)
  status       —— 阶段提示(理解问题/检索/生成)
  rewrite      —— 历史感知改写后的检索问题
  references   —— 引用片段列表,先于正文发出,前端可提前渲染来源
  token        —— 增量正文
  title        —— 首轮问答后生成的会话标题
  done         —— 结束,含耗时、用量、是否命中缓存
  error        —— 出错
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core.deps import client_key, get_current_user
from ..db import get_session, get_sessionmaker
from ..models import Conversation, Message, MessageRole, User
from ..schemas.chat import ChatRequest
from ..services.cache import get_cache
from ..services.rag_chain import answer_stream, generate_title

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["问答"])

# 助手消息用哪个模型名落库(展示用)
DISPLAY_MODEL = settings.chat_model


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _save_assistant_message(
    conversation_id: int,
    content: str,
    references: list[dict[str, Any]] | None,
    usage: dict[str, Any],
    latency_ms: int,
) -> None:
    """把助手回答(含引用快照)落库。

    引用是快照而非外键:知识库后续被改动或删除,历史回答里的引用仍然可追溯。
    """
    async with get_sessionmaker()() as session:
        session.add(
            Message(
                conversation_id=conversation_id,
                role=MessageRole.ASSISTANT,
                content=content,
                references=references or None,
                model=DISPLAY_MODEL,
                prompt_tokens=int(usage.get("input_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or 0),
                latency_ms=latency_ms,
            )
        )
        await session.commit()


async def _touch_conversation(conversation_id: int, title: str | None = None) -> None:
    from datetime import datetime, timezone

    async with get_sessionmaker()() as session:
        conv = await session.get(Conversation, conversation_id)
        if conv is None:
            return
        conv.updated_at = datetime.now(timezone.utc)
        if title:
            conv.title = title
        await session.commit()


@router.post("/stream", summary="流式问答(SSE)")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    # ---------- 限流 ----------
    if settings.rate_limit_enabled:
        allowed, remaining = get_cache().rate_limit(
            client_key(request, user), settings.rate_limit_per_minute, 60
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="提问过于频繁,请稍后再试",
                headers={"Retry-After": "60"},
            )

    # ---------- 解析或新建会话 ----------
    if payload.conversation_id is not None:
        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.id == payload.conversation_id,
                    Conversation.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    else:
        # 不再绑定知识库:检索范围是全部库,由打分排序决定谁被引用
        conv = Conversation(
            user_id=user.id,
            title=payload.question[:20],
        )
        session.add(conv)
        await session.commit()
        await session.refresh(conv)

    conv_id = conv.id

    # ---------- 取历史(在写入本次提问之前) ----------
    history_rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.id.desc())
            .limit(settings.history_rewrite_turns * 2)
        )
    ).scalars().all()
    history = [
        {"role": m.role.value, "content": m.content} for m in reversed(history_rows)
    ]
    is_first_question = len(history) == 0

    # ---------- 保存用户提问 ----------
    session.add(
        Message(
            conversation_id=conv_id,
            role=MessageRole.USER,
            content=payload.question,
        )
    )
    await session.commit()

    async def event_generator() -> AsyncIterator[str]:
        yield _sse({"type": "conversation", "conversation_id": conv_id})

        parts: list[str] = []
        references: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        latency_ms = 0
        errored = False
        # done 先扣下不转发:后面还要生成会话标题,而 done 必须是最后一个事件,
        # 前端收到它就代表本次问答彻底结束。
        done_event: dict[str, Any] | None = None

        try:
            async for event in answer_stream(payload.question, history):
                etype = event.get("type")
                if etype == "token":
                    parts.append(event["content"])
                elif etype == "references":
                    references = event["references"]
                elif etype == "done":
                    usage = event.get("usage") or {}
                    latency_ms = int(event.get("latency_ms") or 0)
                    done_event = event
                    continue
                elif etype == "error":
                    errored = True
                # status/rewrite/references/token/error 原样转发给前端
                yield _sse(event)

        except asyncio.CancelledError:
            # 用户主动停止生成:把已生成的部分落库,不能让这次提问凭空消失
            logger.info("会话 %s 的生成被客户端中断", conv_id)
            if parts:
                await _save_assistant_message(
                    conv_id, "".join(parts) + "\n\n(已停止生成)", references, usage, latency_ms
                )
            await _touch_conversation(conv_id)
            raise

        answer = "".join(parts)
        if answer and not errored:
            await _save_assistant_message(conv_id, answer, references, usage, latency_ms)

        await _touch_conversation(conv_id)

        # 首轮问答后生成会话标题,让侧栏显示有意义的名称
        if is_first_question and answer:
            try:
                title = await generate_title(payload.question)
                await _touch_conversation(conv_id, title)
                yield _sse({"type": "title", "conversation_id": conv_id, "title": title})
            except Exception as e:
                logger.warning("生成会话标题失败: %s", e)

        # 放在最后:前端收到 done 即可认为本轮问答结束
        if done_event is not None:
            yield _sse(done_event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关闭 Nginx 等反代的缓冲,否则 SSE 会被攒着一次性下发
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/stop/{conversation_id}", summary="停止生成")
async def stop_generation(
    conversation_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    """前端通过 abort 掉 SSE 请求即可停止生成,此接口仅用于显式确认会话归属。"""
    conv = (
        await session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return {"message": "已停止"}
