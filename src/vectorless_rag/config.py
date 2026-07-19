from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    environment: str = "development"
    release: str = "dev"
    database_url: str = "postgresql+psycopg://rag_app:rag@localhost:5432/rag"
    database_admin_url: str = "postgresql+psycopg://rag_owner:rag@localhost:5432/rag"
    sql_database_url: str = "postgresql+psycopg://rag_reader:rag@localhost:5432/rag"
    deepseek_api_key: SecretStr | None = None
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_input_cost_per_million_usd: float | None = None
    deepseek_output_cost_per_million_usd: float | None = None
    pageindex_model: str = "deepseek/deepseek-v4-pro"
    pageindex_source_dir: Path = Path("/opt/pageindex")
    pageindex_work_dir: Path = Path("/workspaces")
    pageindex_version: str = "190f8b378be58199ca993566a9214dba72089c54"
    metadata_prompt_version: str = "metadata-v1"
    arxiv_pdf_dir: Path = Path("arxiv-pdfs")
    object_store_endpoint: str = "http://localhost:9000"
    object_store_access_key: SecretStr = SecretStr("minio")
    object_store_secret_key: SecretStr = SecretStr("minio-change-me")
    object_store_bucket: str = "vectorless-rag"
    object_store_region: str = "us-east-1"
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    api_key_pepper: SecretStr = SecretStr("development-only-change-me")
    request_deadline_seconds: int = 120
    upload_max_bytes: int = 200 * 1024 * 1024
    candidate_limit: int = 50
    document_limit: int = 8
    page_ranges_per_document: int = 4
    fetched_page_limit: int = 24
    evidence_token_limit: int = 40_000
    sql_row_limit: int = 200
    sql_timeout_ms: int = 5_000
    sql_lock_timeout_ms: int = 1_000
    sql_payload_limit_bytes: int = 100_000
    sql_explain_cost_ceiling: float = 100_000
    pilot_document_limit: int = 10
    allow_full_corpus_index: bool = False
    worker_poll_seconds: float = 2.0
    extraction_min_characters: int = 500

    @field_validator("arxiv_pdf_dir")
    @classmethod
    def normalize_corpus_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("candidate_limit", "document_limit", "fetched_page_limit")
    @classmethod
    def positive_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("limits must be positive")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
