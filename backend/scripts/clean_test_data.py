"""清理联调测试留下的数据。

api_test.py / e2e_check.py 会注册临时账号(student*、e2e_user_*),
删用户会级联删掉其会话与消息;但它们不删账号,跑几次就会在管理仪表盘上
攒出一堆陌生用户。这个脚本负责收尾。

默认只删测试账号(前缀可识别,不会误伤真实用户)。
admin 名下的会话需要显式加 --conversations 才删 —— 因为那会连带删掉
真实使用产生的对话,不能默认执行。

用法:
    .venv/Scripts/python.exe backend/scripts/clean_test_data.py                 # 只清测试账号
    .venv/Scripts/python.exe backend/scripts/clean_test_data.py --conversations # 同时清 admin 的会话
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.compat import setup_event_loop_policy  # noqa: E402
from app.db import get_sessionmaker, init_engine  # noqa: E402

# 测试脚本固定用这些前缀注册账号,据此识别残留
TEST_USER_PATTERNS = ("e2e_user_%", "student%")


async def main() -> int:
    parser = argparse.ArgumentParser(description="清理联调测试数据")
    parser.add_argument(
        "--conversations",
        action="store_true",
        help="同时删除 admin 名下的全部会话(仅用于演示/测试库)",
    )
    args = parser.parse_args()

    await init_engine()

    async with get_sessionmaker()() as session:
        removed_users = (
            await session.execute(
                text(
                    "DELETE FROM users WHERE "
                    + " OR ".join(f"username LIKE :p{i}" for i in range(len(TEST_USER_PATTERNS)))
                    + " RETURNING username"
                ),
                {f"p{i}": p for i, p in enumerate(TEST_USER_PATTERNS)},
            )
        ).scalars().all()
        print(f"测试账号:删除 {len(removed_users)} 个")
        if removed_users:
            print("  " + ", ".join(sorted(removed_users)))

        if args.conversations:
            removed_convs = (
                await session.execute(
                    text(
                        "DELETE FROM conversations WHERE user_id = "
                        "(SELECT id FROM users WHERE username = 'admin') RETURNING id"
                    )
                )
            ).scalars().all()
            print(f"admin 会话:删除 {len(removed_convs)} 个")

        await session.commit()

        print("\n清理后剩余:")
        for label, sql in (
            ("用户", "SELECT count(*) FROM users"),
            ("知识库", "SELECT count(*) FROM knowledge_bases"),
            ("文档", "SELECT count(*) FROM documents"),
            ("分块", "SELECT count(*) FROM chunks"),
            ("会话", "SELECT count(*) FROM conversations"),
            ("消息", "SELECT count(*) FROM messages"),
        ):
            print(f"  {label}: {(await session.execute(text(sql))).scalar_one()}")
        print("  账号:", (await session.execute(
            text("SELECT username, role FROM users ORDER BY id"))).all())

    return 0


if __name__ == "__main__":
    # 必须在 asyncio.run 之前切换:Windows 默认的 ProactorEventLoop 不能跑 psycopg 异步
    setup_event_loop_policy()
    sys.exit(asyncio.run(main()))
