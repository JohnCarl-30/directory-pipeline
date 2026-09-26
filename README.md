# Directory Pipeline

An agentic **crawl → extract → enrich → resolve → search** pipeline: durable
Temporal workflows over a Python/FastAPI service, writing into an alias-fronted
OpenSearch index.

Two things it deliberately does not do — an agentic extractor, and Pydantic AI —
were evaluated rather than assumed, and the reasoning is in
[`docs/decisions.md`](docs/decisions.md).

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
| Measured relevance | `api/static/index.html` | A search console that renders the facet aggregations the engine ranked with, and the BM25 scoring tree behind any hit — so relevance is inspectable rather than argued about. |
| Extraction accuracy | `scripts/eval_extraction.py` | Per-layer, per-field precision and recall against the fixtures the pages are rendered from. The cascade is a measurement, not a claim. |

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
make install     # uv sync from uv.lock -- exact pinned versions
make demo        # full pipeline against in-process mocks
make test        # 182 tests (~9s; the OpenSearch ones skip without a cluster)

python scripts/eval_extraction.py    # extraction accuracy, per layer and field
```

### Full stack

```bash
make up          # OpenSearch + Dashboards + Temporal + 2 workers + API
make crawl       # POST /ingest/crawl
make search      # GET /search?q=analytics
make reindex     # POST /ingest/reindex  (zero-downtime alias swap)
make down

./scripts/verify_stack.sh    # boot the real containers and assert they work
```

| Service | URL |
|---|---|
| **Search console** | http://localhost:8000/ui |
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
byte-identical. A test pins that ordering, because when it breaks there is no
error — just a larger bill.

**How much the model actually earns.** `scripts/eval_extraction.py` measures it.
The fixtures are the ground truth: the mock site renders its pages *from* Company
records, so the right answer for every field is known. Each company is rendered
under all three templates, which turns 12 records into 36 pages spanning rich
markup, prose-only and near-empty.

```
                recall   precision   missed   wrong
  microdata     100.0%      100%         0       0
  drifted        98.2%      100%         2       0
  stub          100.0%      100%         0       0   (99 fields simply absent)

  DOM selectors        135 fields
  text fallback added   97 fields
  still missing          2   <- the only work the model could justify
  incorrect values       0
```

Facts a page never prints are excluded from recall: a stub listing genuinely has
no phone number, and no layer — model included — could recover one. Counting
those as misses overstated the case for the model layer by nearly three times
before the accounting was fixed.

So the deterministic layers recover **232 of 234 recoverable fields with zero
incorrect values**, and the model is needed for two. Those two are headcounts
written as bare numbers — *"has around 500 and has operated since 1998"* names no
unit, so 500 could be revenue or square footage. A pattern loose enough to catch
it would emit wrong values, which this layer treats as worse than emitting none.
Genuine ambiguity from context is what the model is for.

One caveat stated plainly: this measures fixtures written alongside the
selectors. Real-world markup is messier and these numbers will not transfer
intact. What does transfer is the method and the zero-error result.

An agentic extractor was built and measured before this shape was settled on: it
cost **$2.56 for one company**, made six tool calls including three 404s, and read
an unrelated company's page. [`docs/decisions.md`](docs/decisions.md) has the run,
the reasoning, and the limits of that evidence.

### The search console

`GET /ui` — one self-contained HTML file served by FastAPI. A Python service
does not need a `node_modules` tree and a build step to render a search page,
and the page talks only to the same public endpoints any other client would.

It exists because neither Temporal's web UI nor OpenSearch Dashboards shows what
this pipeline is *for*. Temporal shows workflow execution; Dashboards shows the
index. Neither shows ranked results, why a document scored what it did, or which
records were merged as duplicates.

Facet counts and the size bands come from the existing aggregations rather than
being recomputed in the browser, so the numbers on screen are the numbers
OpenSearch ranked with. Expanding a result calls `/search/explain` and renders
the scoring tree, which makes the BM25 boosts visible instead of a bare score.
Ticking *include duplicates* reveals the records entity resolution folded away —
the Cascade Freight pair is the interesting case, since they look near-identical
and are correctly kept apart.

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

`/metrics/summary` does the arithmetic `/metrics` leaves to the reader:
throughput, the model's share of records, prompt-cache effectiveness, dependency
health, and cost. Two decisions in there are worth knowing:

- **An absent measurement reports as `null`, not `0.0`.** A cache hit rate of
  zero and no lookups at all are different facts, and rendering the second as the
  first makes a freshly started process look broken.
- **Cached tokens are billed at the cache rate.** Charging a 90%-cached prompt
  entirely as fresh input overstates cost roughly fivefold.

Cost is omitted until `LLM_COST_*_PER_MTOK` are set. A stale hardcoded token
price reported as fact is worse than no figure.

The endpoint also reports what it cannot see. In-memory counters belong to the
process answering the request, and the pipeline's work happens in workers — so an
API-served summary shows zero throughput while the pipeline indexes normally. The
response carries a `scope` block saying so, because unqualified "0 records/minute"
reads as an outage. A shared collector is the real fix.

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

182 tests. 173 need neither network nor Docker; the remaining 9 are OpenSearch
integration tests that **skip themselves** when no cluster is reachable.

The model-calling paths are covered without an API key. Both classes resolve
their client lazily into `self._client`, so a fake assigned there is all the
injection required — 13 tests cover refusals (a 200 with no usable content, where
reading `content[0]` would raise), truncation discarded rather than half-parsed,
tool-use decisions, and a batch surviving one pair that raises.

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

### Building the image is not running it

`./scripts/verify_stack.sh` boots the containers that would actually ship and
asserts nine things: `/readyz` reports ready, the worker logged `worker.starting`,
its log holds no traceback, it has not restarted, a crawl reaches the index, and
`/search` and `/ui` answer. On failure it dumps container states and logs before
tearing down. Locally it keeps your volumes; under CI it wipes them. It runs as
its own CI job.

This exists because CI used to build the worker image and stop there, which
proves an image compiles, not that it boots. See the third bug below.

Measured on the demo data, hammering search concurrently through a reindex:

```
reindex: companies-v3-...-5414bd -> companies-v3-...-a21665
         copied=12  swapped=True  old_dropped=False  in 2.09s
concurrent reads during swap: 101 ok, 0 failed
rollback target still exists: True
```

Bugs in this repo found by tests that talk to a real dependency rather than by
the unit suite, which is roughly the point of having them — and one found by
nothing at all, which is why the integration job now exists:

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
- **Found by nothing, for eleven days.** `run_worker()` called
  `worker.run(shutdown_event=stop)`, and `Worker.run()` takes no arguments in any
  released `temporalio`. Both worker containers died on a `TypeError` and
  crash-looped, so the Dockerised worker had never once started: any workflow
  begun through `make up` sat in `RUNNING` forever with nothing to execute it.
  The unit suite drives workflows through the time-skipping environment and never
  calls `run_worker()`; the in-process smoke run never touches a worker; the build
  job only compiled the image. All three stayed green. `verify_stack.sh` was
  written to close that gap, and confirmed against this bug by reverting the fix —
  it fails with the exact `TypeError`.
- **A retry policy that named a class nobody raises.** `NON_RETRYABLE` listed
  `"PermanentFetchError"`, which exists nowhere. Temporal matches these against
  `type(exc).__name__`, so it matched nothing, and the class that *does* escape —
  `FetchError` — was unlisted. A 404 was retried six times over ten minutes. The
  verdict now travels with the exception (`ApplicationError(non_retryable=not
  exc.retryable)`), using the classification the client already made, and a test
  asserts every `NON_RETRYABLE` entry names an importable exception.
- **A reindex retry started a second copy beside the first.**
  `physical_index_name()` is unique by construction, so a retry computed a new
  target, created a second index and submitted a second copy task while the
  original was still running — two full copies competing for IO, and an orphan
  index nothing would swap to. The copy task outlives its submitter, so the
  activity now records the task id and target in every heartbeat and reattaches.
  It refuses a partial resume: a task id without its target would reattach to a
  copy filling one index while swapping the alias to another, publishing an empty
  one.

---

## Known limitations

- Every accuracy number here is measured against fixtures written alongside the
  selectors. The method transfers; the percentages will not.
- The enrichment cache is in-process. The interface (`get`/`set` with TTL) is
  the one you put Redis behind.
- Metrics are in-process counters, so a summary served by the API cannot see
  worker-side activity. A Prometheus exporter is the fix — same call sites,
  different sink.
- Single-shard index, suited to the demo's data volume rather than copied as a
  default.
- Verified locally only: no cloud deployment, and nothing here has run against a
  real directory site.
