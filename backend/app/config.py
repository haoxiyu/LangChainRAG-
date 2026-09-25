"""全局配置。所有可调参数集中在此,通过 .env 覆盖。"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录
BACKEND_DIR = Path(__file__).resolve().parent.parent
# 项目根目录
ROOT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 阿里云百炼 ----------
    dashscope_api_key: str = ""
    # 兼容模式(OpenAI 协议):对话 + 向量化
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # 原生接口:重排序只能走这里(兼容模式返回 404)
    dashscope_native_base: str = "https://dashscope.aliyuncs.com/api/v1/services"

    chat_model: str = "qwen-plus"
    embedding_model: str = "qwen3.7-text-embedding"
    rerank_model: str = "qwen3.7-text-rerank"
    # 实测 qwen3.7-text-embedding 输出 1024 维,建表与索引必须与此一致
    embedding_dim: int = 1024

    # ---------- 数据库 ----------
    # 留空则自动用 pgembed 启动项目内嵌的 PostgreSQL(免安装、免 Docker)
    database_url: str = ""
    # 内嵌 PG 的数据目录
    pgdata_dir: Path = ROOT_DIR / ".pgdata"
    # 业务库名
    db_name: str = "ragdb"
    # 连接池。压测发现:每个问答请求的 get_session 依赖会贯穿整个流式回答
    # (几十秒)一直占着连接,检索阶段还会另开稠密/稀疏两个独立会话,
    # 峰值约 3~5 条/请求 —— 默认 30 条上限在 60 并发左右就会耗尽,
    # 之后请求排队等 pool_timeout 秒再抛 TimeoutError(表现为 500)。
    db_pool_size: int = 10
    db_max_overflow: int = 20
    # 池满后等连接的超时秒数,超时抛错。默认与 SQLAlchemy 一致
    db_pool_timeout: int = 30

    # ---------- 鉴权 ----------
    secret_key: str = "change-me-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 天,方便毕设演示

    # ---------- 管理员种子账号 ----------
    admin_username: str = "admin"
    admin_password: str = "123456"

    # ---------- 检索参数 ----------
    chunk_size: int = 500
    chunk_overlap: int = 80
    # 稠密/稀疏两路各召回多少
    retrieve_top_k: int = 20
    # 重排后送入 prompt 的片段数
    rerank_top_k: int = 5
    # RRF 融合常数
    rrf_k: int = 60
    # 是否启用重排序(关掉可省 API 调用)
    enable_rerank: bool = True
    # 历史感知改写:取最近几轮对话做指代消解
    history_rewrite_turns: int = 4

    # ---------- 缓存 ----------
    cache_dir: Path = ROOT_DIR / ".cache"
    cache_enabled: bool = True
    # 语义缓存相似度阈值(0~1,越高越严格)
    semantic_cache_threshold: float = 0.95
    semantic_cache_ttl: int = 3600

    # ---------- 限流 ----------
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 30

    # ---------- 上传 ----------
    upload_dir: Path = ROOT_DIR / "uploads"
    max_upload_mb: int = 50

    # ---------- CORS ----------
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_extensions(self) -> set[str]:
        # 与 services/parsers 里实际实现的解析器保持一致
        return {".pdf", ".docx", ".txt", ".md", ".xlsx", ".csv"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
