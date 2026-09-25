"""配置项的单元测试。

配置类里的两个「派生属性」是运行时才解析的字符串,写错了不会在启动时报错,
只会在某个具体请求上表现得莫名其妙:
- cors_origins 解析错了 → 前端跨域被拦,浏览器只给一句 CORS 报错;
- allowed_extensions 与解析器不一致 → 上传成功的文件在解析阶段才失败。

所以这里直接对着属性断言,并且把「上传白名单」和「解析器实际支持」绑在一起。
"""

from __future__ import annotations

from app.config import Settings, settings


def test_cors_origins_are_split_and_trimmed() -> None:
    s = Settings(cors_origins="http://localhost:5173, http://127.0.0.1:5173 ")
    assert s.cors_origin_list == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_cors_origins_ignore_blank_entries() -> None:
    """手工编辑 .env 时很容易多出逗号,空串会变成一个永远匹配不到的白名单项。"""
    s = Settings(cors_origins="http://a.com,,  ,http://b.com")
    assert s.cors_origin_list == ["http://a.com", "http://b.com"]


def test_cors_origins_can_be_empty() -> None:
    s = Settings(cors_origins="")
    assert s.cors_origin_list == []


def test_allowed_extensions_match_upload_whitelist() -> None:
    assert settings.allowed_extensions == {".pdf", ".docx", ".txt", ".md", ".xlsx", ".csv"}


def test_allowed_extensions_are_lowercase_dotted() -> None:
    """扩展名统一成小写带点形式,才能和上传文件名直接比对。"""
    for ext in settings.allowed_extensions:
        assert ext.startswith(".")
        assert ext == ext.lower()


def test_defaults_for_retrieval_budget() -> None:
    """召回 20 → 重排留 5,这个「先宽后严」的比例是效果与成本的折中,
    改动它等于改变检索效果,应当是有意识的决定而不是手滑。
    """
    assert settings.retrieve_top_k == 20
    assert settings.rerank_top_k == 5
    assert settings.rerank_top_k < settings.retrieve_top_k


def test_chunk_overlap_is_smaller_than_chunk_size() -> None:
    """重叠大于块长会让切分永不前进(死循环或无限分块)。"""
    assert 0 <= settings.chunk_overlap < settings.chunk_size


def test_embedding_dim_matches_pgvector_column() -> None:
    """chunks.embedding 建表时写的是 vector(1024),与配置必须一致。"""
    assert settings.embedding_dim == 1024
