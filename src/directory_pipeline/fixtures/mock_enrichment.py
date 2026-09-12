"""A fake enrichment API that misbehaves the way real ones do.

Specifically it:
  * enforces a token-bucket rate limit and returns 429 + Retry-After
  * fails ~8% of requests with a 503 (transient, retryable)
  * honours Idempotency-Key, returning the cached response for a repeat
  * requires bearer auth
  * returns 200 with {"status": "not_found"} rather than a 404 for misses

That last one is deliberate: plenty of vendor APIs signal "no data" with a 200
body, and code that only checks status codes silently treats it as a hit.
"""

from __future__ import annotations

import random
import time
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, Header, Response
from fastapi.responses import JSONResponse

from .data import ENRICHMENT

app = FastAPI(title="Mock Enrichment API")

RATE_PER_SECOND = 12.0
BURST = 20
FAILURE_RATE = 0.08

_tokens = BURST
_last_refill = time.monotonic()
_idempotency: dict[str, dict[str, Any]] = {}
_call_counts: dict[str, int] = defaultdict(int)


def _take_token() -> bool:
    global _tokens, _last_refill
    now = time.monotonic()
    _tokens = min(BURST, _tokens + (now - _last_refill) * RATE_PER_SECOND)
    _last_refill = now
    if _tokens >= 1:
        _tokens -= 1
        return True
    return False


@app.get("/v1/companies/lookup")
async def lookup(
    domain: str | None = None,
    name: str | None = None,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    if not authorization or not authorization.startswith("Bearer "):
        return JSONResponse({"error": "missing bearer token"}, status_code=401)

    # Idempotency short-circuits everything, including the rate limit -- a
    # retry of a request we already answered costs the caller nothing.
    if idempotency_key and idempotency_key in _idempotency:
        return JSONResponse(_idempotency[idempotency_key], headers={"X-Idempotent-Replay": "true"})

    if not _take_token():
        return JSONResponse(
            {"error": "rate limited"}, status_code=429, headers={"Retry-After": "1"}
        )

    if random.random() < FAILURE_RATE:
        return JSONResponse({"error": "upstream unavailable"}, status_code=503)

    key = (domain or "").lower()
    _call_counts[key or f"name:{name}"] += 1

    record = ENRICHMENT.get(key)
    if record is None:
        payload: dict[str, Any] = {"status": "not_found", "query": {"domain": domain, "name": name}}
    else:
        payload = {"status": "ok", "provider": "demo-enrichment", **record}

    if idempotency_key:
        _idempotency[idempotency_key] = payload
    return JSONResponse(payload)


@app.get("/v1/_stats")
async def stats() -> dict[str, Any]:
    """Proves the cache and single-flight are working: billable calls per key."""
    return {
        "billable_calls": dict(_call_counts),
        "total_billable_calls": sum(_call_counts.values()),
        "idempotency_keys_seen": len(_idempotency),
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8082, log_level="warning")


if __name__ == "__main__":
    run()
