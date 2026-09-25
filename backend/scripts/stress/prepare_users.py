"""压测数据准备:批量造账号 + 铸 token,并支持一键清理。

为什么不让压测脚本走注册/登录接口:
1. 注册限流 10 次/分钟/IP、登录 20 次/分钟/IP(auth.py),100 个账号得跑十分钟;
2. 登录要过 bcrypt(cost=12,单次约百毫秒)且是**同步调用**,100 并发登录会把
   单线程事件循环整个卡住 —— 那时测到的是 bcrypt,不是 RAG。

所以这里直接写库、直接签 JWT。password_hash 只算一次再复用:这些账号只为压测
存在,不需要各自独立的哈希(而且逐一 gensalt 又要多花十几秒)。

用法:
    .venv/Scripts/python.exe backend/scripts/stress/prepare_users.py            # 造 100 个
    .venv/Scripts/python.exe backend/scripts/stress/prepare_users.py -n 50      # 造 50 个
    .venv/Scripts/python.exe backend/scripts/stress/prepare_users.py --cleanup  # 清理
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import asyncio
import csv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from app.compat import setup_event_loop_policy  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.db import get_sessionmaker, init_engine  # noqa: E402

# 前缀必须与 clean_test_data.py 的 TEST_USER_PATTERNS 对得上,否则清理脚本漏掉它们
USER_PREFIX = "stress_user_"
SHARED_PASSWORD = "StressTest123"
CSV_PATH = Path(__file__).resolve().parent / "stress_users.csv"


async def create_users(n: int) -> int:
    await init_engine()

    # 只算一次哈希,100 个账号共用 —— 逐一算要多花十几秒 bcrypt
    shared_hash = hash_password(SHARED_PASSWORD)

    rows: list[tuple[str, str, int]] = []
    async with get_sessionmaker()() as s:
        existing = set(
            (
                await s.execute(
                    text("SELECT username FROM users WHERE username LIKE :p"),
                    {"p": f"{USER_PREFIX}%"},
                )
            ).scalars()
        )
        if existing:
            print(f"已有 {len(existing)} 个压测账号,先清理再重建")
            await s.execute(
                text("DELETE FROM users WHERE username LIKE :p"), {"p": f"{USER_PREFIX}%"}
            )
            await s.commit()

        for i in range(n):
            username = f"{USER_PREFIX}{i:03d}"
            uid = (
                await s.execute(
                    text(
                        "INSERT INTO users (username, password_hash, role, created_at) "
                        "VALUES (:u, :h, 'user', now()) RETURNING id"
                    ),
                    {"u": username, "h": shared_hash},
                )
            ).scalar_one()
            rows.append((username, "", uid))
        await s.commit()

    # 拿到 id 后再签 token —— sub 必须是真实用户 id,get_current_user 靠它查库
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["user_id", "username", "token"])
        for username, _, uid in rows:
            token = create_access_token(uid, extra={"username": username, "role": "user"})
            w.writerow([uid, username, token])

    print(f"已创建 {len(rows)} 个压测账号,token 写入 {CSV_PATH.name}")
    print(f"  账号范围: {USER_PREFIX}000 ~ {USER_PREFIX}{n - 1:03d}")
    print(f"  共享密码: {SHARED_PASSWORD}(仅备用,压测直接用 CSV 里的 token)")
    return len(rows)


async def cleanup() -> int:
    await init_engine()
    async with get_sessionmaker()() as s:
        # 删用户会级联删掉其会话与消息(cascade)
        names = (
            await s.execute(
                text("DELETE FROM users WHERE username LIKE :p RETURNING username"),
                {"p": f"{USER_PREFIX}%"},
            )
        ).scalars().all()
        await s.commit()
        print(f"已删除 {len(names)} 个压测账号(及其会话与消息)")
        for label, sql in (
            ("用户", "SELECT count(*) FROM users"),
            ("会话", "SELECT count(*) FROM conversations"),
            ("消息", "SELECT count(*) FROM messages"),
        ):
            print(f"  剩余{label}: {(await s.execute(text(sql))).scalar_one()}")

    if CSV_PATH.exists():
        CSV_PATH.unlink()
        print(f"已删除 {CSV_PATH.name}")
    return len(names)


def main() -> int:
    parser = argparse.ArgumentParser(description="压测数据准备")
    parser.add_argument("-n", "--users", type=int, default=100, help="创建的账号数")
    parser.add_argument("--cleanup", action="store_true", help="删除全部压测账号")
    args = parser.parse_args()

    if args.cleanup:
        asyncio.run(cleanup())
    else:
        asyncio.run(create_users(args.users))
    return 0


if __name__ == "__main__":
    # Windows 默认 ProactorEventLoop 跑不了 psycopg 异步,必须在 asyncio.run 之前切
    setup_event_loop_policy()
    sys.exit(main())
