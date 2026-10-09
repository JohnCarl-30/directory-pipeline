"""Single source of truth for runtime configuration.

Everything is env-driven so the same image runs locally, in ECS, and in tests.
Settings are read once and cached; activities and the API share the instance.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
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

    # Politeness. On by default: a crawler that has to be configured into
    # following the rules is a crawler that ships not following them.
    obey_robots: bool = True
    robots_cache_ttl_s: float = 3600.0

    # Browser-string rotation. Off, and incompatible with obey_robots -- see
    # the validator. Kept because it is genuinely useful against a host you
    # own (our own mock, a staging target) when you are testing how the client
    # behaves under fingerprint churn.
    rotate_user_agents: bool = False

    # Agentic extraction
    anthropic_api_key: str = ""
    extraction_model: str = "claude-opus-5"
    extraction_mode: str = "auto"  # auto | llm | dom

    # Temporal SDK metrics: task-queue latency, activity failures, poller
    # counts. Off unless a port is given, because enabling it binds one -- a
    # local script that only wanted a client should not open a listener. The
    # compose stack sets it for the worker and the API.
    temporal_metrics_port: int = 0

    # Worker metrics. The workers do the pipeline's work but have no HTTP
    # server, so an exporter served only by the API reports a busy pipeline as
    # idle. 0 disables the listener.
    worker_metrics_port: int = 9100

    # Cost estimation, in USD per million tokens. Unset on purpose: token
    # prices change, and /metrics/summary reporting a stale hardcoded rate as
    # fact would be worse than reporting nothing. Fill these from current
    # published pricing for whichever model extraction_model names.
    llm_cost_input_per_mtok: float = 0.0
    llm_cost_output_per_mtok: float = 0.0
    llm_cost_cache_read_per_mtok: float = 0.0

    @field_validator("proxy_pool", mode="before")
    @classmethod
    def _split_pool(cls, v: object) -> object:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @model_validator(mode="after")
    def _identity_must_be_coherent(self) -> Settings:
        """Refuse to both obey robots.txt and lie about who is asking.

        robots.txt groups are selected by the product token in `user_agent`, so
        with rotation on we would read the rules written for
        `directory-pipeline` and then send requests claiming to be Chrome. The
        host's logs would show traffic it has no rules for, from an agent that
        is, in its records, ignoring robots.txt entirely.

        This fails at startup rather than warning, because the two settings are
        individually reasonable and the combination is not -- which is exactly
        the shape of bug that survives a code review and is discovered by
        somebody else's abuse desk.
        """
        if self.obey_robots and self.rotate_user_agents:
            raise ValueError(
                "ROTATE_USER_AGENTS=true is incompatible with OBEY_ROBOTS=true: "
                "robots.txt rules are matched against the product token in USER_AGENT, "
                "so rotating browser strings would claim rules the requests do not "
                "identify as. Either leave rotation off (obey robots, identify "
                "honestly), or set OBEY_ROBOTS=false to opt out explicitly -- which is "
                "only defensible against a host you own."
            )
        return self

    @property
    def opensearch_auth(self) -> tuple[str, str] | None:
        if self.opensearch_user:
            return (self.opensearch_user, self.opensearch_password)
        return None

    @property
    def llm_cost_rates(self) -> dict[str, float] | None:
        """Configured token rates, or None when none were supplied."""
        rates = {
            "input": self.llm_cost_input_per_mtok,
            "output": self.llm_cost_output_per_mtok,
            "cache_read": self.llm_cost_cache_read_per_mtok,
        }
        return rates if any(rates.values()) else None

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
