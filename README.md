# Directory Pipeline

An agentic **crawl → extract → enrich → resolve → search** pipeline: durable
Temporal workflows over a Python/FastAPI service, writing into an alias-fronted
OpenSearch index.

It runs end-to-end with **no Docker, no credentials, and no external services** —
mock upstreams ship with the repo:

```bash
make install
make demo
```

That exercises every real component. Only the *orchestration* is swapped out,
which is exactly the layer Temporal owns.

---

## What it demonstrates

| Concern | Where | The decision worth reading |
|---|---|---|
| Agentic extraction | `extraction/agent.py` | Three layers, cheapest first — selectors, then regex over prose, then the model. Structured outputs make the model's response schema-valid by construction; confidence is capped by the weakest source used. |
| Tool calling | `resolution/adjudicator.py` | A strict tool schema to adjudicate *only* borderline duplicate pairs, so model cost tracks genuine ambiguity rather than corpus size. |
| Index design | `search/index.py` | `dynamic: strict` mappings, three analyzers for company names, alias-swap reindex. |
| Zero-downtime reindex | `search/index.py::reindex` | Build new → copy → refresh → **atomic** alias swap → keep the old index as the rollback path. |
| Relevance tuning | `search/query.py` | Filter vs. query context, per-field boosts, a deliberately gentle completeness multiplier. |
| Durable orchestration | `orchestration/` | Child workflow per batch for blast-radius containment; per-failure-shape retry policies; heartbeats; `continue_as_new`. |
| Safe fan-out | `scraping/`, `enrichment/` | Token bucket, circuit breaker, full-jitter backoff, proxy rotation, idempotency keys, single-flight dedupe, TTL cache. |
| Entity resolution | `resolution/entity.py` | Multi-key blocking, weighted probabilistic scoring, union-find clustering. |
| Data QA | `reporting/qa.py` | Pandas coverage/validity gates that block a release, not just decorate it. |

---

## Architecture

```
                    ┌──────────────── Temporal ────────────────┐
                    │                                          │
  FastAPI  ──────▶  │  CrawlDirectoryWorkflow                  │
  POST /ingest/crawl│      ├── discover_listings (per category)│
                    │      └── ProcessBatchWorkflow  × N       │
                    │             fetch+extract → enrich       │
                    │             → resolve → index            │
                    │                                          │
                    │  ReindexWorkflow → atomic alias swap     │
                    └──────────────────────┬───────────────────┘
                                           │
   directory site ◀── ResilientClient ─────┤      ┌────────────────┐
   enrichment API ◀── (limit/breaker/retry)└────▶ │  OpenSearch    │
                                                  │  alias:companies│
   FastAPI GET /search ───────────────────────▶   └────────────────┘
```

**Why a child workflow per batch.** One poisoned batch of 25 companies exhausts
its own retries and fails alone; the other 400 batches still index. A single
flat workflow would take the whole crawl down with it, and its history would
grow past the limit on a large run.

---

## Running it

### No dependencies

```bash
make install     # uv venv + editable install
make demo        # full pipeline against in-process mocks
make test        # 144 tests (~3s without OpenSearch; ~9s with)
```

### Full stack

```bash
make up          # OpenSearch + Dashboards + Temporal + 2 workers + API
make crawl       # POST /ingest/crawl
make search      # GET /search?q=analytics
make reindex     # POST /ingest/reindex  (zero-downtime alias swap)
make down
```

| Service | URL |
|---|---|
| API docs | http://localhost:8000/docs |
| Temporal UI | http://localhost:8080 |
| OpenSearch Dashboards | http://localhost:5601 |

### Enabling the agentic paths

Both model-backed paths are **off** unless a key is present, and the pipeline is
fully functional without one:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make demo
```

With a key set, the LLM extractor handles pages the first two layers could not,
and the adjudicator resolves borderline duplicate pairs.

---

## Design notes

### Extraction: pay for the model only where it earns its cost

A directory page is 95% boilerplate. Selectors extract it for free and cannot
hallucinate; regex over the page's prose catches the common drifted-template
case. Only when a *critical* field is still missing does a page reach the model.

Each layer fills only what the layer above it missed, and confidence is capped
by the weakest source used — a selector hit is ground truth, a regex hit a
strong inference, a model output a weaker one. Nothing overwrites a more
reliable source.

The model call uses `output_config.format` with a JSON schema, so the response
is schema-valid by construction: no regex repair, no retry-on-parse loop. The
system prompt is cached (identical across every page in a crawl); the page and
the missing-field list go *after* the cache breakpoint so the cached prefix stays
byte-identical.

### Entity resolution: blocking, then weighted scoring

Comparing every record to every other is O(n²) — 100k companies is 5 billion
comparisons. Records are compared only when they share a cheap blocking key
(domain, phone, email, name-prefix + locality, sorted-token fingerprint).
Several independent keys give recall: a record with a typo'd name still blocks
on its phone number.

Scoring is a weighted sum of field agreements, because fields differ enormously
in what agreement tells you. Two signals are worth calling out:

- **Domain disagreement scores negative**, not zero. Two companies rarely share
  a website, so *different* domains is evidence against a match.
- **Shared domain across different localities scores negative** (`location_conflict`).
  Every branch of a chain lists the same corporate website. Without this,
  domain agreement alone merges a whole multi-site company into one record —
  the demo data includes exactly this case (Cascade Freight, Portland and
  Seattle), and it lands in the review band for the adjudicator rather than
  being silently merged.

Duplicates are indexed with `duplicate_of` set rather than deleted: dropping
them loses the provenance that proves the merge was right. Search filters them
out with `is_canonical`.

### Zero-downtime reindex

Applications never name an index — they talk to an alias.

```
create companies-v4-<ts> → copy from v3 → refresh → update_aliases(add v4, remove v3)
```

`update_aliases` applies its actions atomically, so no request ever sees zero or
two indices behind the alias. The old index is **kept by default**: it is the
rollback path (`POST /ingest/index/rollback`).

One caveat named honestly: writes landing on the source index *after* the copy
begins are not carried over. In production you either pause the writer for the
swap or dual-write during the copy.

### Fan-out that upstreams survive

Composition order in `ResilientClient` is the whole point:

```
circuit breaker → rate limiter → request → classify → backoff
```

The breaker comes first so an already-failing host costs nothing. The limiter
comes before the request so we never *send* over budget — limiting after the
fact just means getting 429'd politely. Backoff uses **full jitter**: without
it, 50 coroutines that hit one 503 retry in lockstep forever and keep the host
down. A 429 pushes the whole host into cooldown, so one coroutine's discovery
slows all of its siblings.

Enrichment adds idempotency keys (a retry after a timeout cannot double-charge),
single-flight coalescing (200 records at one parent company produce one billable
lookup), and negative caching. The demo prints the billable-call count from the
mock provider so the effect is visible: 12 records, 9 upstream calls.

### Observability

Structured JSON logs with workflow/run ids bound, so one company can be traced
from fetch through enrichment to the indexed document. `/metrics` exposes
counters and p50/p95/p99 latencies. Liveness (`/healthz`) and readiness
(`/readyz`) are split — conflating them means an OpenSearch blip restarts every
pod.

---

## Layout

```
src/directory_pipeline/
├── config.py             env-driven settings
├── observability.py      structured logging + metrics
├── domain/models.py      versioned contracts crossing every boundary
├── scraping/             rate_limit · circuit · client · crawler
├── extraction/           normalize (deterministic) · agent (3-layer)
├── enrichment/           idempotency, single-flight, TTL cache
├── resolution/           entity (blocking+scoring) · adjudicator (tool calling)
├── search/               index (mappings, reindex) · query (relevance)
├── orchestration/        activities · workflows · worker · retry policies
├── reporting/qa.py       pandas coverage/validity gates
├── api/                  FastAPI: search, ingest, ops
└── fixtures/             mock directory + mock enrichment API
```

## Testing

144 tests. 135 need neither network nor Docker and run in ~3s; the remaining 9
are OpenSearch integration tests that **skip themselves** when no cluster is
reachable.

Workflow tests use Temporal's `WorkflowEnvironment.start_time_skipping()`, so
retry backoffs that would take minutes complete instantly, with fake activities
isolating the orchestration logic. Failure injection is keyed on URL rather than
attempt count — keying on attempts only simulates a blip the retry policy
legitimately recovers from, and the test would then be asserting that retries
*don't* work. Both cases are covered: a permanently failing batch is contained
and reported; a transient one is absorbed silently.

`timeout = 120` is set in pytest config: a hung test is a failed test, not a
blocked CI run.

The integration tests cover what a unit test structurally cannot — that the
mappings are actually accepted, that the analyzers tokenize the way the query
assumes, and that the alias swap is atomic and reversible:

```bash
docker compose up -d opensearch
pytest tests/test_search_integration.py
```

Measured on the demo data, hammering search concurrently through a reindex:

```
reindex: companies-v3-...-5414bd -> companies-v3-...-a21665
         copied=12  swapped=True  old_dropped=False  in 2.09s
concurrent reads during swap: 101 ok, 0 failed
rollback target still exists: True
```

Two bugs in this repo were found by tests that talk to a real dependency rather
than by the unit suite, which is roughly the point of having them:

- **Found by the OpenSearch integration tests.** `physical_index_name()` used a
  second-granularity timestamp, so a bootstrap followed immediately by a
  reindex produced the *same* name. `create_index` treats an existing index as
  success (it must, for the bootstrap to be idempotent), so the collision was
  silent and the reindex then tried to read and write one index. Fixed with a
  random suffix plus an explicit source-equals-target guard.
- **Found by the Temporal workflow tests.** Activities are invoked by string
  name, so Temporal had no return annotation to read and deserialized payloads
  into raw `dict`s. The resulting `AttributeError` is a *workflow task*
  failure, which Temporal retries forever — so the symptom was a workflow that
  hung rather than one that errored. Fixed by passing `result_type` on every
  typed activity call.

---

## Known limitations

- Reindex copies with `wait_for_completion=True`; at real scale you take the
  task handle and poll it.
- The enrichment cache is in-process. The interface (`get`/`set` with TTL) is
  the one you put Redis behind.
- Metrics are in-process counters, not a Prometheus exporter — same call sites,
  different sink.
- Single-shard index, suited to the demo's data volume rather than copied as a
  default.
