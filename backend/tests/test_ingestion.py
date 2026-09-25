"""摄取环节里可离线验证的纯逻辑:token 计数与落盘路径。

依赖关系:count_tokens 用于统计展示,失败必须降级成字符数而不是让整份文档入库失败;
document_file_path 用文档 id 命名落盘文件,后缀来自用户上传的文件名 —— 这两处都很小,
但一旦出错(比如 tiktoken 抛异常没接住)整份文档就会变成 failed 状态。
"""

from __future__ import annotations

import tiktoken
import pytest

from app.config import settings
from app.services.ingestion import count_tokens, document_file_path


# ---------- token 计数 ----------
def test_count_tokens_returns_positive_int() -> None:
    n = count_tokens("七天无理由退货")
    assert isinstance(n, int)
    assert n > 0


def test_count_tokens_is_monotonic() -> None:
    """更长的文本必然不少于更短的,否则统计面板上的数字会自相矛盾。"""
    assert count_tokens("电池") <= count_tokens("电池容量5000mAh,支持67W快充")


def test_count_tokens_falls_back_to_char_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """tiktoken 拿不到编码(离线/首次下载失败)时退化为字符数,不能抛出去。"""
    def _boom(_name: str):
        raise RuntimeError("encoding not available")

    monkeypatch.setattr(tiktoken, "get_encoding", _boom)
    assert count_tokens("一二三四五") == 5


def test_count_tokens_handles_empty_text() -> None:
    assert count_tokens("") == 0


# ---------- 落盘路径 ----------
def test_file_path_is_named_by_document_id() -> None:
    """用文档 id 命名天然去重:两个用户上传同名文件不会互相覆盖。"""
    path = document_file_path(42, "商品说明.md")
    assert path.parent == settings.upload_dir
    assert path.name == "42.md"


def test_file_path_lowercases_suffix() -> None:
    """上传 .PDF 也要能落到 .pdf,否则解析器按后缀分发时会找不到处理器。"""
    assert document_file_path(7, "说明书.PDF").name == "7.pdf"
    assert document_file_path(7, "说明.DOCX").name == "7.docx"


def test_file_path_keeps_only_last_suffix() -> None:
    """「商品.v2.md」这类名字不能把中间的点也当成后缀。"""
    assert document_file_path(9, "商品.v2.md").name == "9.md"


def test_file_path_without_suffix() -> None:
    """无后缀文件不该抛异常,交给上层按扩展名白名单拒绝即可。"""
    assert document_file_path(11, "无后缀文件").name == "11"
