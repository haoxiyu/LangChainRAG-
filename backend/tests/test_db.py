"""数据库层的纯逻辑单元测试:连接串转换 + pgembed 句柄记录清理。

to_async_url 决定了 SQLAlchemy 用哪个驱动:pgembed 给的是同步串
(postgresql://…),而本项目全程异步,必须换成 psycopg3 的异步驱动。
换错前缀会得到一个「连不上」或「同步驱动跑在事件循环里」的报错,
排查成本远高于三个断言,所以单独锁住。

_prune_dead_handles 是启动路径上的自愈逻辑,详见 app/db.py 里它的 docstring。
"""

from __future__ import annotations

import json

import pytest

from app import db
from app.db import to_async_url


@pytest.mark.parametrize(
    ("sync_url", "expected"),
    [
        # pgembed / psycopg2 常见的三种写法都要能识别
        ("postgresql://postgres@127.0.0.1:5000/ragdb", "postgresql+psycopg://postgres@127.0.0.1:5000/ragdb"),
        ("postgres://postgres@127.0.0.1:5000/ragdb", "postgresql+psycopg://postgres@127.0.0.1:5000/ragdb"),
        ("postgresql+psycopg2://postgres@127.0.0.1:5000/ragdb", "postgresql+psycopg://postgres@127.0.0.1:5000/ragdb"),
    ],
)
def test_sync_urls_are_rewritten(sync_url: str, expected: str) -> None:
    assert to_async_url(sync_url) == expected


def test_already_async_url_is_untouched() -> None:
    """幂等:重复调用不能把前缀越改越长(postgresql+psycopg+psycopg…)。"""
    url = "postgresql+psycopg://postgres@127.0.0.1:5000/ragdb"
    assert to_async_url(url) == url
    assert to_async_url(to_async_url(url)) == url


def test_unknown_scheme_is_returned_as_is() -> None:
    """不认识的写法原样返回,让 SQLAlchemy 自己报清晰的错,而不是被改坏。"""
    assert to_async_url("mysql://root@localhost/db") == "mysql://root@localhost/db"


# ---------- pgembed 句柄记录清理 ----------
def _handles_file(pgdata):
    return pgdata / ".handle_pids.json"


def _write_handles(pgdata, pids: list[int]) -> None:
    _handles_file(pgdata).write_text(json.dumps(pids))


def test_prune_keeps_live_pids_and_drops_dead_ones(tmp_path, monkeypatch) -> None:
    """死 pid 不剔掉,pgembed 会永远认为「还有别人在用」,数据库再也不会被正常关闭。

    后果是一串 postgres.exe 常驻不退,且每次强杀后启动都要走 30 秒的崩溃恢复。
    """
    _write_handles(tmp_path, [11, 22, 33])
    monkeypatch.setattr(db.psutil, "pid_exists", lambda pid: pid == 22)

    db._prune_dead_handles(tmp_path)

    assert json.loads(_handles_file(tmp_path).read_text()) == [22]


def test_prune_leaves_file_untouched_when_all_alive(tmp_path, monkeypatch) -> None:
    """全都活着时不该写盘 —— 启动路径上多余的写入没有意义。"""
    _write_handles(tmp_path, [11, 22])
    before = _handles_file(tmp_path).stat().st_mtime_ns
    monkeypatch.setattr(db.psutil, "pid_exists", lambda pid: True)

    db._prune_dead_handles(tmp_path)

    assert json.loads(_handles_file(tmp_path).read_text()) == [11, 22]
    assert _handles_file(tmp_path).stat().st_mtime_ns == before


def test_prune_without_handles_file_is_safe(tmp_path) -> None:
    """首次启动时这个文件还不存在,不能因此抛异常。"""
    db._prune_dead_handles(tmp_path)
    assert not _handles_file(tmp_path).exists()


@pytest.mark.parametrize("broken", ["{不是合法 json", '"一个字符串"', "123"])
def test_prune_survives_broken_handles_file(tmp_path, broken: str) -> None:
    """文件损坏或类型不对时安静跳过。

    这是启动路径,清理失败绝不能把整个应用带不起来 —— 宁可留着脏数据。
    """
    _handles_file(tmp_path).write_text(broken)

    db._prune_dead_handles(tmp_path)  # 不抛异常即通过
