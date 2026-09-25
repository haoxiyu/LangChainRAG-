"""平台兼容处理。

Windows 上 asyncio 默认使用 ProactorEventLoop,而 psycopg 的异步模式
不支持它(会抛 InterfaceError)。必须在事件循环创建之前切换到
SelectorEventLoop。

本模块必须在任何 asyncio 事件循环启动前被导入并调用。
"""

from __future__ import annotations

import asyncio
import sys


def setup_event_loop_policy() -> None:
    """在 Windows 上切换到 SelectorEventLoop,使 psycopg 异步可用。

    适用于自己调用 asyncio.run() 的脚本。
    uvicorn 不走事件循环策略,必须改用下面的 selector_loop_factory。
    """
    if sys.platform == "win32":
        policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        if policy is not None and not isinstance(asyncio.get_event_loop_policy(), policy):
            asyncio.set_event_loop_policy(policy())


def selector_loop_factory(*_args: object, **_kwargs: object) -> asyncio.AbstractEventLoop:
    """给 uvicorn 用的 loop factory:返回一个 SelectorEventLoop 实例。

    uvicorn 0.36+ 在 Windows 且非子进程模式下会硬编码使用 ProactorEventLoop,
    而 psycopg 的异步模式不支持它。通过 uvicorn 的 loop 参数指定本工厂可强制
    使用 SelectorEventLoop:

        uvicorn.run("app.main:app", loop="app.compat:selector_loop_factory")

    注意必须返回「实例」而不是类:uvicorn 对自定义 loop 路径是直接拿函数当工厂
    调用(不像内置路径那样会补 use_subprocess 参数),返回类会导致
    Runner 把一个类当成事件循环使用。
    """
    return asyncio.SelectorEventLoop()
