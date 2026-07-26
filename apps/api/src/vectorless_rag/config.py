from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False, env_ignore_empty=True
    )

    environment: str = "development"
    release: str = "dev"
    database_url: str = "postgresql+psycopg://rag_app:rag@localhost:5432/rag"
    database_admin_url: str = "postgresql+psycopg://rag_owner:rag@localhost:5432/rag"
    sql_database_url: str = "postgresql+psycopg://rag_reader:rag@localhost:5432/rag"
    deepseek_api_key: SecretStr | None = None
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_input_cost_per_million_usd: float | None = None
    deepseek_cache_hit_input_cost_per_million_usd: float | None = None
    deepseek_output_cost_per_million_usd: float | None = None
    pageindex_model: str = "deepseek/deepseek-v4-pro"
    pageindex_source_dir: Path = Path("/opt/pageindex")
    pageindex_work_dir: Path = Path("/workspaces")
    pageindex_version: Literal["190f8b378be58199ca993566a9214dba72089c54+vr4"] = (
        "190f8b378be58199ca993566a9214dba72089c54+vr4"
    )
    pageindex_llm_max_attempts: int = 3
    pageindex_async_concurrency: int = 8
    pageindex_max_output_tokens: int = 8_192
    query_node_selection_max_output_tokens: int = 4_096
    pageindex_call_timeout_seconds: int = 120
    pageindex_timeout_seconds: int = 3_600
    ingestion_max_attempts: int = 3
    ingestion_lease_seconds: int = 120
    ingestion_heartbeat_seconds: int = 30
    ingestion_stale_grace_seconds: int = 300
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
    request_deadline_seconds: int = 180
    upload_max_bytes: int = 200 * 1024 * 1024
    candidate_limit: int = 50
    catalog_batch_token_limit: int = 24_000
    document_limit: int = 8
    page_ranges_per_document: int = 4
    fetched_page_limit: int = 24
    evidence_token_limit: int = 40_000
    sql_row_limit: int = 200
    sql_timeout_ms: int = 5_000
    sql_lock_timeout_ms: int = 1_000
    sql_payload_limit_bytes: int = 100_000
    sql_explain_cost_ceiling: float = 100_000
    pilot_document_limit: int = 25
    pilot_cost_limit_usd: float = 10.0
    allow_full_corpus_index: bool = False
    worker_poll_seconds: float = 2.0
    extraction_min_characters: int = 500

    @field_validator("arxiv_pdf_dir")
    @classmethod
    def normalize_corpus_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator(
        "candidate_limit",
        "document_limit",
        "fetched_page_limit",
        "pageindex_llm_max_attempts",
        "pageindex_async_concurrency",
        "pageindex_max_output_tokens",
        "query_node_selection_max_output_tokens",
        "pageindex_call_timeout_seconds",
        "pageindex_timeout_seconds",
        "ingestion_max_attempts",
        "ingestion_lease_seconds",
        "ingestion_heartbeat_seconds",
        "ingestion_stale_grace_seconds",
        "catalog_batch_token_limit",
        "request_deadline_seconds",
    )
    @classmethod
    def positive_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("limits must be positive")
        return value

    @field_validator(
        "deepseek_input_cost_per_million_usd",
        "deepseek_cache_hit_input_cost_per_million_usd",
        "deepseek_output_cost_per_million_usd",
    )
    @classmethod
    def non_negative_prices(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("prices must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def valid_runtime_intervals(self) -> Settings:
        if self.ingestion_heartbeat_seconds >= self.ingestion_lease_seconds:
            raise ValueError("ingestion heartbeat must be shorter than the lease")
        if not math.isfinite(self.pilot_cost_limit_usd) or self.pilot_cost_limit_usd <= 0:
            raise ValueError("pilot cost limit must be finite and positive")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
