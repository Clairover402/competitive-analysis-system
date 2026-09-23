"""配置管理 — 通过 pydantic-settings 读取环境变量与 .env 文件。

环境变量覆盖 .env 文件的值。所有字段都有合理的默认值。
"""

from __future__ import annotations

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """竞品分析系统全局配置。

    读取顺序：环境变量 > .env 文件 > 默认值。
    使用方式：settings = Settings(); api_key = settings.deepseek_api_key
    """

    model_config = SettingsConfigDict(
        env_file=str(__import__("pathlib").Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- LLM: DeepSeek Chat API ----
    deepseek_api_key: str = "competitive-analysis-system-key"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # ---- Database: PostgreSQL + pgvector ----
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_database: str = "competitive_analysis"
    pg_user: str = "postgres"
    pg_password: str = "competitive_analysis_pwd"

    # ---- Embedding: BGE-M3 (1024维) ----
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"

    # ---- Embedding/Rerank API（SiliconFlow 硅基流动）----
    # 【ECS 2G 内存方案】True = 走在线 API，不加载本地 ~2GB 模型；
    # False = 走本地 BGE-M3 / reranker 模型（原逻辑，需 ≥4G 内存）
    embedding_api_enabled: bool = False
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"

    # ---- Reranker: BGE-reranker-v2-m3 ----
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

    # ---- Auth: JWT ----
    jwt_secret: str = ""  # 必须通过 .env 或环境变量设置，不给默认值
    jwt_expire_hours: int = 24

    # ---- Limits ----
    max_concurrent_collectors: int = 3
    max_rounds_supervisor: int = 10
    token_bucket_capacity: int = 100
    llm_rpm_limit: int = 60

    @computed_field
    @property
    def database_url(self) -> str:
        """拼接 asyncpg 连接字符串。

        Returns:
            PostgreSQL DSN，格式：postgresql://user:password@host:port/database
        """
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )