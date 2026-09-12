"""Single source of truth for runtime configuration.

Everything is env-driven so the same image runs locally, in ECS, and in tests.
Settings are read once and cached; activities and the API share the instance.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Temporal
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "directory-pipeline"

    # OpenSearch
    opensearch_url: str = "http://localhost:9200"
    opensearch_user: str = ""
    opensearch_password: str = ""
    opensearch_alias: str = "companies"

    # Upstream targets
    directory_base_url: str = "http://localhost:8081"
    enrichment_base_url: str = "http://localhost:8082"
    enrichment_api_key: str = "demo-key"

    # Throughput / politeness
    crawl_rps: float = 5.0
    crawl_burst: int = 10
    crawl_concurrency: int = 8
    enrich_rps: float = 10.0
    enrich_concurrency: int = 16

    # Egress
    proxy_pool: list[str] = Field(default_factory=list)
    request_timeout_s: float = 20.0
    user_agent: str = "directory-pipeline/0.1 (+https://example.com/bot; contact=devs@example.com)"

    # Agentic extraction
    anthropic_api_key: str = ""
    extraction_model: str = "claude-opus-5"
    extraction_mode: str = "auto"  # auto | llm | dom

    @field_validator("proxy_pool", mode="before")
    @classmethod
    def _split_pool(cls, v: object) -> object:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @property
    def opensearch_auth(self) -> tuple[str, str] | None:
        if self.opensearch_user:
            return (self.opensearch_user, self.opensearch_password)
        return None

    @property
    def llm_extraction_enabled(self) -> bool:
        if self.extraction_mode == "dom":
            return False
        if self.extraction_mode == "llm":
            return True
        return bool(self.anthropic_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
