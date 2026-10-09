"""The MCP server.

Two layers. The unit tests inject a fake SearchClient, so they assert the tool
contract -- argument translation, result shaping, error handling -- with no
cluster and no subprocess. The stdio test launches the real entry point the way
a desktop client does and skips itself without OpenSearch, because that is the
only way to catch the failure mode unique to this transport: anything written to
stdout that is not JSON-RPC corrupts the stream, and a stray print or a logger
pointed at stdout does exactly that.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import httpx
import pytest

from directory_pipeline.config import Settings
from directory_pipeline.mcp.server import _brief, build_server

OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")


def _reachable() -> bool:
    try:
        return httpx.get(f"{OPENSEARCH_URL}/_cluster/health", timeout=2).status_code == 200
    except Exception:
        return False


def settings() -> Settings:
    return Settings(
        directory_base_url="http://directory.test",
        enrichment_base_url="http://enrich.test",
    )


class FakeSearch:
    """Records the kwargs the tool passed, returns a canned response."""

    def __init__(self, response: dict[str, Any] | None = None, raises: Exception | None = None):
        self.response = response or {"total": 0, "took_ms": 1, "results": [], "facets": {}}
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.response

    async def get(self, record_id: str) -> dict[str, Any] | None:
        self.calls.append({"get": record_id})
        if self.raises:
            raise self.raises
        return {"record_id": record_id, "name": "Acme"} if record_id == "known" else None

    async def explain(self, record_id: str, q: str) -> dict[str, Any]:
        self.calls.append({"explain": record_id, "q": q})
        if self.raises:
            raise self.raises
        return {"explanation": {"value": 1.25, "description": "sum of:"}}

    async def aclose(self) -> None: ...


async def call(server: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await server.call_tool(tool, args)
    payload = result[1] if isinstance(result, tuple) else result
    if isinstance(payload, dict):
        return payload
    text = payload.content[0].text
    parsed: dict[str, Any] = json.loads(text)
    return parsed


@pytest.fixture
def server_with(monkeypatch: pytest.MonkeyPatch):
    """Build a server whose lazily-resolved SearchClient is a fake.

    monkeypatch rather than direct assignment so the substitution is undone
    after each test -- a module-level patch left in place would follow the fake
    into every later test in the session.
    """

    def factory(fake: FakeSearch) -> Any:
        import directory_pipeline.mcp.server as mod

        monkeypatch.setattr(mod, "SearchClient", lambda *a, **k: fake)
        return build_server(settings())

    return factory


# --------------------------------------------------------------------------
# result shaping
# --------------------------------------------------------------------------
def test_brief_keeps_what_a_reader_needs_and_drops_the_rest():
    """A tool result is spent from the client's context window."""
    hit = {
        "record_id": "r1",
        "name": "Northwind Analytics, Inc.",
        "employee_count": 138,
        "founded_year": 2014,
        "address": {"city": "Austin", "region": "TX", "country": "US", "postal_code": "78701"},
        "contact": {"website": "https://northwindanalytics.com", "phone_e164": "+15125550142"},
        "enrichment": {"industry": "Software", "revenue_usd": 24000000, "confidence": 0.8},
        "categories": ["Analytics", "Data Platform", "Retail", "SaaS", "Cloud"],
        "description": "x" * 400,
        "_score": 14.5436,
        "is_canonical": True,
    }
    out = _brief(hit)

    assert out["name"] == "Northwind Analytics, Inc."
    assert out["location"] == "Austin, TX"
    assert out["industry"] == "Software"
    assert out["score"] == 14.544
    assert len(out["categories"]) == 4, "category lists are capped"
    assert "description" not in out, "the 400-char description is the point of trimming"
    assert "enrichment" not in out and "address" not in out
    assert "duplicate_of" not in out, "only non-canonical records carry this"


def test_brief_surfaces_which_record_a_duplicate_resolved_to():
    out = _brief(
        {
            "record_id": "r2",
            "name": "Cascade Freight Systems Corp.",
            "is_canonical": False,
            "duplicate_of": "r1",
        }
    )
    assert out["duplicate_of"] == "r1"


def test_brief_omits_fields_that_are_absent_rather_than_emitting_nulls():
    out = _brief({"record_id": "r3", "name": "Sparse Co"})
    assert set(out) == {"record_id", "name"}


# --------------------------------------------------------------------------
# tool contract
# --------------------------------------------------------------------------
async def test_all_four_tools_are_advertised():
    tools = await build_server(settings()).list_tools()
    assert {t.name for t in tools} == {
        "search_companies",
        "get_company",
        "explain_ranking",
        "index_status",
    }
    # The description is what a model reads to decide whether to call it.
    assert all(t.description for t in tools)


async def test_include_duplicates_inverts_into_canonical_only(server_with):
    fake = FakeSearch()
    srv = server_with(fake)
    await call(srv, "search_companies", {"q": "x", "include_duplicates": True})
    assert fake.calls[-1]["canonical_only"] is False

    await call(srv, "search_companies", {"q": "x"})
    assert fake.calls[-1]["canonical_only"] is True


async def test_size_is_clamped_so_one_call_cannot_dump_the_index(server_with):
    fake = FakeSearch()
    srv = server_with(fake)
    await call(srv, "search_companies", {"q": "x", "size": 5000})
    assert fake.calls[-1]["size"] == 50
    await call(srv, "search_companies", {"q": "x", "size": 0})
    assert fake.calls[-1]["size"] == 1


async def test_a_cluster_outage_returns_an_error_field_not_a_traceback(server_with):
    """A tool that raises gives the client a protocol error and no explanation."""
    srv = server_with(FakeSearch(raises=ConnectionError("connection refused")))
    out = await call(srv, "search_companies", {"q": "x"})
    assert "error" in out
    assert "connection refused" in out["error"]


async def test_get_company_says_so_when_there_is_no_such_record(server_with):
    srv = server_with(FakeSearch())
    assert "error" in await call(srv, "get_company", {"record_id": "missing"})
    assert (await call(srv, "get_company", {"record_id": "known"}))["name"] == "Acme"


async def test_full_detail_returns_the_untrimmed_documents(server_with):
    fake = FakeSearch(
        {
            "total": 1,
            "took_ms": 3,
            "facets": {},
            "results": [{"record_id": "r1", "name": "Acme", "description": "y" * 300}],
        }
    )
    srv = server_with(fake)
    brief = await call(srv, "search_companies", {"q": "x"})
    full = await call(srv, "search_companies", {"q": "x", "detail": "full"})
    assert "description" not in brief["results"][0]
    assert len(full["results"][0]["description"]) == 300


# --------------------------------------------------------------------------
# the transport
# --------------------------------------------------------------------------
@pytest.mark.skipif(not _reachable(), reason=f"no OpenSearch at {OPENSEARCH_URL}")
def test_the_server_speaks_stdio_without_corrupting_the_stream():
    """Launch the real entry point the way a desktop client does.

    The failure mode unique to stdio is that stdout is the protocol. A print()
    left in a handler, or a logger defaulting to stdout, breaks every client
    with a parse error that points nowhere near the cause. Only a subprocess
    test catches it.
    """
    entry = os.path.join(".venv", "bin", "dp-mcp")
    if not os.path.exists(entry):
        pytest.skip("dp-mcp is not installed in .venv")
    proc = subprocess.Popen(
        [entry],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def send(msg: dict[str, Any]) -> None:
        assert proc.stdin
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def await_id(want: int) -> dict[str, Any]:
        assert proc.stdout
        while True:
            line = proc.stdout.readline()
            assert line, "server closed the stream before answering"
            # Every line on stdout must be JSON-RPC. This is the assertion.
            msg: dict[str, Any] = json.loads(line)
            if msg.get("id") == want:
                return msg

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            }
        )
        assert await_id(1)["result"]["serverInfo"]["name"] == "directory-pipeline"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "index_status", "arguments": {}},
            }
        )
        status = json.loads(await_id(2)["result"]["content"][0]["text"])
        assert status["reachable"] is True
        assert status["alias"]
    finally:
        assert proc.stdin
        proc.stdin.close()
        proc.wait(timeout=30)

    assert proc.returncode == 0
    assert "Unclosed" not in (proc.stderr.read() if proc.stderr else ""), (
        "the lifespan hook should close the OpenSearch connections"
    )
