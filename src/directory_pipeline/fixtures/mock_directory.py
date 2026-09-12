"""A fake business directory to crawl.

Three page templates, because a real directory is never one template:

  microdata -- clean schema.org markup; the DOM extractor handles it perfectly
  drifted   -- the same data in prose with no microdata; DOM extraction finds
               the name and little else, so the LLM path (if enabled) earns
               its keep here
  stub      -- near-empty listing; should end up low-confidence and sparse

It also returns 429 on a burst, so the token bucket and Retry-After handling
have something real to react to.
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from .data import BY_CATEGORY, BY_SLUG, Company

app = FastAPI(title="Mock Business Directory")

PAGE_SIZE = 4
_hits: dict[str, list[float]] = defaultdict(list)
BURST_LIMIT = 40  # requests...
BURST_WINDOW_S = 1.0  # ...per second, per client


@app.middleware("http")
async def burst_limiter(request: Request, call_next):
    """Return a real 429 with Retry-After when hammered."""
    client = request.client.host if request.client else "anon"
    now = time.monotonic()
    window = _hits[client]
    window[:] = [t for t in window if now - t < BURST_WINDOW_S]
    if len(window) >= BURST_LIMIT:
        return Response(content="rate limited", status_code=429, headers={"Retry-After": "1"})
    window.append(now)
    return await call_next(request)


def _index_html(category: str, page: int) -> str:
    companies = BY_CATEGORY.get(category, [])
    start = (page - 1) * PAGE_SIZE
    chunk = companies[start : start + PAGE_SIZE]
    has_next = start + PAGE_SIZE < len(companies)

    rows = "\n".join(
        f'    <li class="listing">'
        f'<a class="listing-link" href="/company/{c.slug}">{c.name}</a>'
        f'<span class="locality">{c.city}</span></li>'
        for c in chunk
    )
    next_link = (
        f'<a rel="next" class="next-page" href="/directory/{category}?page={page + 1}">Next</a>'
        if has_next
        else ""
    )
    return f"""<!doctype html>
<html><head><title>{category} directory - page {page}</title></head>
<body>
  <nav><a href="/">Home</a></nav>
  <h1>{category.title()} companies</h1>
  <ul class="listings">
{rows}
  </ul>
  {next_link}
</body></html>"""


def _microdata_page(c: Company) -> str:
    tags = "".join(f'<span class="tag" itemprop="category">{t}</span>' for t in c.tags)
    return f"""<!doctype html>
<html><head><title>{c.name}</title>
<meta name="description" content="{c.description}"></head>
<body>
<nav><a href="/">Home</a> &middot; <a href="/directory/{c.category}">Back</a></nav>
<article itemscope itemtype="https://schema.org/Organization">
  <h1 class="company-name" itemprop="name">{c.name}</h1>
  <p class="company-description" itemprop="description">{c.description}</p>
  <div class="tags">{tags}</div>
  <div itemprop="address" itemscope itemtype="https://schema.org/PostalAddress">
    <span class="street-address" itemprop="streetAddress">{c.street}</span>,
    <span class="locality" itemprop="addressLocality">{c.city}</span>,
    <span class="region" itemprop="addressRegion">{c.region}</span>
    <span class="postal-code" itemprop="postalCode">{c.postal}</span>
  </div>
  <p class="phone" itemprop="telephone">{c.phone}</p>
  <p><a itemprop="email" href="mailto:{c.email}">{c.email}</a></p>
  <p><a class="website" itemprop="url" href="{_href(c.website)}">Visit site</a></p>
  <p class="employees" itemprop="numberOfEmployees">{c.employees}</p>
  <p class="founded" itemprop="foundingDate">{c.founded}</p>
</article>
<aside><h3>Related listings</h3><ul>
  <li><a href="/company/harbor-point-labs">Harbor Point Labs</a></li>
</ul></aside>
</body></html>"""


def _drifted_page(c: Company) -> str:
    """Same facts, no microdata, details buried in prose."""
    return f"""<!doctype html>
<html><head><title>{c.name} | Directory</title></head>
<body>
<nav><a href="/">Home</a></nav>
<div class="profile-card">
  <h1>{c.name}</h1>
  <div class="blurb">
    <p>{c.description}</p>
    <p>Based at {c.street}, {c.city}, {c.region} {c.postal}.
       Reach the team on {c.phone} or by email at {c.email}.
       More at {c.website}.</p>
    <p>The company has around {c.employees} and has operated since {c.founded}.
       Focus areas include {", ".join(c.tags) if c.tags else "general business"}.</p>
  </div>
</div>
<footer>Listing provided by Mock Directory</footer>
</body></html>"""


def _stub_page(c: Company) -> str:
    return f"""<!doctype html>
<html><head><title>{c.name}</title></head>
<body><nav><a href="/">Home</a></nav>
<h1>{c.name}</h1>
<p>This listing has not been claimed. <a href="/claim">Claim this business</a>.</p>
</body></html>"""


def _href(website: str) -> str:
    if not website:
        return "#"
    return website if website.startswith("http") else f"https://{website}"


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    links = "".join(f'<li><a href="/directory/{cat}">{cat}</a></li>' for cat in sorted(BY_CATEGORY))
    return f"<!doctype html><html><body><h1>Mock Directory</h1><ul>{links}</ul></body></html>"


@app.get("/directory/{category}", response_class=HTMLResponse)
async def directory(category: str, page: int = 1) -> HTMLResponse:
    if category not in BY_CATEGORY:
        return HTMLResponse("<html><body><h1>No such category</h1></body></html>", 404)
    return HTMLResponse(_index_html(category, page))


@app.get("/company/{slug}", response_class=HTMLResponse)
async def company(slug: str) -> HTMLResponse:
    c = BY_SLUG.get(slug)
    if c is None:
        return HTMLResponse("<html><body><h1>Not found</h1></body></html>", 404)
    renderers = {"microdata": _microdata_page, "drifted": _drifted_page, "stub": _stub_page}
    return HTMLResponse(renderers[c.template](c))


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8081, log_level="warning")


if __name__ == "__main__":
    run()
