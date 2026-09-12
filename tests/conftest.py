from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from directory_pipeline.config import Settings  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings: LLM paths off so tests never hit the network."""
    return Settings(
        directory_base_url="http://directory.test",
        enrichment_base_url="http://enrich.test",
        anthropic_api_key="",
        extraction_mode="dom",
        crawl_rps=1000.0,
        crawl_burst=1000,
    )
