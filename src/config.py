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
        env_file=".env",
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

    # ---- Reranker: BGE-reranker-v2-m3 ----
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

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