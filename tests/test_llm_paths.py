"""The model-calling paths, driven by fakes -- no API key, no network, no spend.

These are the branches that only fire against a live model: a refusal, a
truncated response, a tool-use block, a pair that raises mid-batch. They were
the only untested code in the pipeline, which is backwards: they are the
branches most likely to surprise in production, because they depend on another
system's behaviour rather than our own.

Both classes resolve their client lazily into self._client, so a fake assigned
there is all the injection needed.
"""

from __future__ import annotations

from typing import Any

import pytest

from directory_pipeline.config import Settings
from directory_pipeline.domain.models import Address, CompanyRecord, Contact, RawListing
from directory_pipeline.extraction.agent import LLMExtractor
from directory_pipeline.extraction.normalize import normalize_company_name
from directory_pipeline.observability import METRICS
from directory_pipeline.resolution.adjudicator import Adjudicator
from directory_pipeline.resolution.entity import Candidate


# --------------------------------------------------------------------------
# Fakes shaped like the Anthropic response objects the code actually touches.
# --------------------------------------------------------------------------
class FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, name: str, payload: dict[str, Any]) -> None:
        self.name = name
        self.input = payload


class FakeUsage:
    def __init__(self, inp: int, out: int, cache_read: int = 0) -> None:
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_read_input_tokens = cache_read


class FakeResponse:
    def __init__(
        self,
        *,
        stop_reason: str = "end_turn",
        content: list[Any] | None = None,
        usage: FakeUsage | None = None,
    ) -> None:
        self.stop_reason = stop_reason
        self.content = content or []
        self.usage = usage


class FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


class FakeClient:
    def __init__(self, *responses: Any) -> None:
        self.messages = FakeMessages(list(responses))


@pytest.fixture
def llm_settings() -> Settings:
    return Settings(
        directory_base_url="http://directory.test",
        enrichment_base_url="http://enrich.test",
        anthropic_api_key="test-key-not-real",
        extraction_mode="llm",
    )


def listing(html: str = "<html><body>Acme</body></html>") -> RawListing:
    return RawListing(source="test", source_id="acme", url="http://test/acme", html=html)


def record(name: str, *, website: str | None = None) -> CompanyRecord:
    sid = name.lower().replace(" ", "-").replace(",", "").replace(".", "")
    return CompanyRecord(
        record_id=CompanyRecord.make_record_id("test", sid),
        source="test",
        source_id=sid,
        source_url=f"http://test/{sid}",
        name=name,
        name_normalized=normalize_company_name(name),
        address=Address(),
        contact=Contact(website=website),
    )


# --------------------------------------------------------------------------
# LLMExtractor
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_extract_parses_a_schema_constrained_response(llm_settings):
    extractor = LLMExtractor(llm_settings)
    extractor._client = FakeClient(
        FakeResponse(content=[FakeTextBlock('{"name": "Acme Systems, Inc."}')])
    )
    assert await extractor.extract(listing(), missing=["name"]) == {"name": "Acme Systems, Inc."}


@pytest.mark.asyncio
async def test_refusal_yields_no_fields_instead_of_an_index_error(llm_settings):
    """A refusal is a 200 with no usable content; content[0] would raise."""
    extractor = LLMExtractor(llm_settings)
    extractor._client = FakeClient(FakeResponse(stop_reason="refusal", content=[]))
    before = METRICS.snapshot()

    assert await extractor.extract(listing(), missing=[]) == {}

    after = METRICS.snapshot()
    assert after != before  # the refusal was counted, not swallowed silently


@pytest.mark.asyncio
async def test_truncated_response_is_discarded_not_half_parsed(llm_settings):
    """Half a JSON document is worse than nothing: it would parse as wrong data."""
    extractor = LLMExtractor(llm_settings)
    extractor._client = FakeClient(
        FakeResponse(stop_reason="max_tokens", content=[FakeTextBlock('{"name": "Acm')])
    )
    assert await extractor.extract(listing(), missing=[]) == {}


@pytest.mark.asyncio
async def test_response_without_a_text_block_is_empty_not_an_error(llm_settings):
    extractor = LLMExtractor(llm_settings)
    extractor._client = FakeClient(FakeResponse(content=[]))
    assert await extractor.extract(listing(), missing=[]) == {}


@pytest.mark.asyncio
async def test_token_usage_is_recorded_including_cache_reads(llm_settings):
    extractor = LLMExtractor(llm_settings)
    extractor._client = FakeClient(
        FakeResponse(
            content=[FakeTextBlock("{}")],
            usage=FakeUsage(inp=1200, out=80, cache_read=1100),
        )
    )
    await extractor.extract(listing(), missing=[])

    counters = METRICS.snapshot()
    flat = str(counters)
    # Cache reads are the whole point of the prefix ordering; if they stop being
    # recorded, a caching regression becomes invisible.
    assert "llm_input_tokens" in flat
    assert "llm_cache_read_tokens" in flat


@pytest.mark.asyncio
async def test_page_content_stays_out_of_the_cached_prefix(llm_settings):
    """The cached system block must be byte-identical across pages.

    If the page ever moves into the system block the prefix changes every call
    and prompt caching silently stops paying for itself -- no error, just a
    larger bill. This pins the ordering the comment in extract() promises.
    """
    extractor = LLMExtractor(llm_settings)
    extractor._client = FakeClient(
        FakeResponse(content=[FakeTextBlock("{}")]),
        FakeResponse(content=[FakeTextBlock("{}")]),
    )

    await extractor.extract(listing("<html>PAGE-ONE-MARKER</html>"), missing=[])
    await extractor.extract(listing("<html>PAGE-TWO-MARKER</html>"), missing=[])

    first, second = extractor._client.messages.calls
    assert first["system"] == second["system"], "cached prefix drifted between pages"
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "PAGE-ONE-MARKER" not in str(first["system"])
    assert "PAGE-ONE-MARKER" in str(first["messages"])


@pytest.mark.asyncio
async def test_oversized_pages_are_truncated_before_sending(llm_settings):
    from directory_pipeline.extraction.agent import _MAX_HTML_CHARS

    extractor = LLMExtractor(llm_settings)
    extractor._client = FakeClient(FakeResponse(content=[FakeTextBlock("{}")]))

    await extractor.extract(listing("x" * (_MAX_HTML_CHARS * 2)), missing=[])

    content = extractor._client.messages.calls[0]["messages"][0]["content"]
    page = content.split("<page>\n")[1].split("\n</page>")[0]
    assert len(page) == _MAX_HTML_CHARS


# --------------------------------------------------------------------------
# Adjudicator
# --------------------------------------------------------------------------
def candidate(score: float = 0.5) -> Candidate:
    return Candidate(
        left=record("Cascade Freight Systems", website="cascade.example"),
        right=record("Cascade Freight Systems Corp.", website="cascade.example"),
        score=score,
        signals={"shared_domain": 0.55},
    )


def decision(
    verdict: str = "same_company",
    relationship: str = "identical",
    confidence: float = 0.9,
) -> FakeToolUseBlock:
    return FakeToolUseBlock(
        "record_decision",
        {
            "verdict": verdict,
            "relationship": relationship,
            "confidence": confidence,
            "deciding_evidence": "identical domain and phone",
        },
    )


@pytest.mark.asyncio
async def test_adjudicate_returns_the_tool_input(llm_settings):
    adj = Adjudicator(llm_settings)
    adj._client = FakeClient(FakeResponse(content=[decision()]))

    result = await adj.adjudicate(candidate())
    assert result is not None
    assert result["verdict"] == "same_company"
    assert result["confidence"] == 0.9


@pytest.mark.asyncio
async def test_adjudicate_refusal_returns_none(llm_settings):
    adj = Adjudicator(llm_settings)
    adj._client = FakeClient(FakeResponse(stop_reason="refusal", content=[]))
    assert await adj.adjudicate(candidate()) is None


@pytest.mark.asyncio
async def test_adjudicate_ignores_an_unexpected_tool(llm_settings):
    adj = Adjudicator(llm_settings)
    adj._client = FakeClient(
        FakeResponse(content=[FakeToolUseBlock("something_else", {"verdict": "x"})])
    )
    assert await adj.adjudicate(candidate()) is None


@pytest.mark.asyncio
async def test_adjudicate_many_is_a_no_op_without_a_key():
    disabled = Settings(
        directory_base_url="http://directory.test",
        enrichment_base_url="http://enrich.test",
        anthropic_api_key="",
    )
    adj = Adjudicator(disabled)
    assert await adj.adjudicate_many([candidate()]) == []


@pytest.mark.asyncio
async def test_only_confident_identical_matches_are_accepted(llm_settings):
    """A merge is destructive, so the bar is verdict AND relationship AND score."""
    cases = [
        (decision(), True),
        (decision(verdict="different_company"), False),
        (decision(relationship="parent_subsidiary"), False),
        (decision(confidence=0.5), False),
    ]
    for block, should_accept in cases:
        adj = Adjudicator(llm_settings)
        adj._client = FakeClient(FakeResponse(content=[block]))
        accepted = await adj.adjudicate_many([candidate()], min_confidence=0.7)
        assert bool(accepted) is should_accept, f"{block.input} -> {accepted}"


@pytest.mark.asyncio
async def test_one_failing_pair_does_not_sink_the_batch(llm_settings):
    """gather(return_exceptions=True) is only useful if the loop honours it."""
    adj = Adjudicator(llm_settings)
    adj._client = FakeClient(
        RuntimeError("upstream 529 overloaded"),
        FakeResponse(content=[decision()]),
    )
    accepted = await adj.adjudicate_many([candidate(), candidate()])
    assert len(accepted) == 1
